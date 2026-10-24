"""软超时收尾：三个批量入口都必须先置停止信号再上抛（I2 / N1）

为什么：`raise` 之前会先执行 `with ThreadPoolExecutor(...)` 的 `__exit__`，
即 `shutdown(wait=True)` —— 它会等所有排队行跑完，而这些行正卡在
`judge_pool.acquire` 里最长等 wait_timeout（900s）。软超时 6600s / 硬超时
7200s 只剩 600s 余量，等不完就撞硬 SIGKILL：进程被杀时等待者来不及 ZREM
自己，幽灵队首扩散到别的任务。置位 stop_event 后，池等待在 ≤2s 内以
JudgePoolAborted 退出，收尾可达。

N1 补充：软超时信号在主线程任意字节码处抛出，行内 try 只罩住 future.result()，
`as_completed` 阻塞处与 try 块之外的 `_persist_row_result` 同样会中 —— 这些位置
逃逸的异常进不了内层 except，必须由线程池块上的 `_soft_timeout_stops_pool`
守卫兜住（contextmanager 的 __exit__ 后进先出，先于 executor 的 shutdown 执行）。
"""
import threading
import time
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.services.evaluation_executor import EvaluationExecutor

pytestmark = pytest.mark.unit


def _executor(stop_event) -> EvaluationExecutor:
    # task_id=None：跳过进度更新与行级落库（本用例只验停止信号与异常上抛）
    return EvaluationExecutor(max_workers=1, task_id=None, stop_event=stop_event)


def _assert_sets_stop_event_and_reraises(run_batch) -> None:
    stop_event = threading.Event()
    executor = _executor(stop_event)

    with pytest.raises(SoftTimeLimitExceeded):
        run_batch(executor)

    assert stop_event.is_set(), "软超时上抛前必须置停止信号，否则排队行会把收尾拖到硬超时"


def test_execute_batch_soft_timeout_sets_stop_event(monkeypatch):
    def run(executor):
        monkeypatch.setattr(
            executor, "_evaluate_single",
            lambda payload, task: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )
        return executor.execute_batch([{"_orig_idx": 0}], SimpleNamespace())

    _assert_sets_stop_event_and_reraises(run)


def test_execute_batch_rag_soft_timeout_sets_stop_event(monkeypatch):
    def run(executor):
        monkeypatch.setattr(
            executor, "_evaluate_single_rag",
            lambda payload, task: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )
        return executor.execute_batch_rag([{"_orig_idx": 0}], SimpleNamespace())

    _assert_sets_stop_event_and_reraises(run)


def test_execute_batch_latency_soft_timeout_sets_stop_event(monkeypatch):
    def run(executor):
        monkeypatch.setattr(
            executor, "_evaluate_single_latency",
            lambda payload, task: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )
        return executor.execute_batch_latency(
            [{"_orig_idx": 0}], SimpleNamespace(latency_config={})
        )

    _assert_sets_stop_event_and_reraises(run)


def test_soft_timeout_stop_event_reaches_queued_rows(monkeypatch):
    """置位后排队行必须能看到停止信号，收尾才不会被 shutdown(wait=True) 拖住

    真实链路里排队行卡在 judge_pool.acquire，停止信号让它 ≤2s 内以
    JudgePoolAborted 退出；这里用「等 stop_event」复现同一依赖。
    """
    stop_event = threading.Event()
    executor = _executor(stop_event)
    seen = []

    def _fake_single(payload, task):
        if payload.get("first"):
            raise SoftTimeLimitExceeded()
        got = stop_event.wait(5)
        seen.append(got)
        return {"idx": payload["_orig_idx"], "success": False, "error": "任务已停止"}

    monkeypatch.setattr(executor, "_evaluate_single", _fake_single)

    started = time.monotonic()
    with pytest.raises(SoftTimeLimitExceeded):
        executor.execute_batch(
            [{"_orig_idx": 0, "first": True}, {"_orig_idx": 1}], SimpleNamespace()
        )
    elapsed = time.monotonic() - started

    assert seen == [True], "排队行没收到停止信号：shutdown(wait=True) 会等它到 wait_timeout"
    assert elapsed < 5, f"收尾被排队行拖住（{elapsed:.2f}s），停止信号没起到快速退出作用"


# ---------------------------------------------------------------------------
# N1：软超时还会从行内 try **之外**逃逸（as_completed 阻塞处 / _persist_row_result）
#
# Celery 的软超时信号在主线程任意字节码处抛出。行内 try 只罩住 future.result()，
# 而 `_persist_row_result` 在 try 块之后调用（execute_batch:139 / rag:1033 /
# latency:1113），`for future in as_completed(...)` 自身也阻塞。这两处中的
# SoftTimeLimitExceeded 不会进内层 except，只能靠线程池块外的
# _soft_timeout_stops_pool 守卫在 executor.__exit__（shutdown(wait=True)）之前置位。
# ---------------------------------------------------------------------------


class _BrokenStopEvent(threading.Event):
    """set() 抛 Redis 异常：生产的 RedisStopSignalChecker.set() 就是一次裸 Redis 写"""

    def set(self):
        raise ConnectionError("redis down")


def _persist_path_executor(stop_event, monkeypatch, single="_evaluate_single"):
    """task_id + execution_number 齐备，_persist_row_result 会真被调用"""
    executor = EvaluationExecutor(
        max_workers=1, task_id="task-n1", stop_event=stop_event, execution_number=1
    )
    monkeypatch.setattr(
        executor, single,
        lambda payload, task: {"idx": payload["_orig_idx"], "success": True},
    )
    monkeypatch.setattr(executor, "_update_progress", lambda *a, **k: None)
    return executor


def _assert_persist_escape_sets_stop_event(monkeypatch, single, run_batch) -> None:
    stop_event = threading.Event()
    executor = _persist_path_executor(stop_event, monkeypatch, single)

    def _boom(pos, payload, result):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(executor, "_persist_row_result", _boom)

    with pytest.raises(SoftTimeLimitExceeded):
        run_batch(executor)

    assert stop_event.is_set(), (
        "落库处逃逸的软超时没置停止信号：内层 except 罩不到这里，"
        "shutdown(wait=True) 会把收尾拖到硬超时"
    )


def test_execute_batch_soft_timeout_from_persist_sets_stop_event(monkeypatch):
    _assert_persist_escape_sets_stop_event(
        monkeypatch, "_evaluate_single",
        lambda ex: ex.execute_batch([{"_orig_idx": 0}], SimpleNamespace()),
    )


def test_execute_batch_rag_soft_timeout_from_persist_sets_stop_event(monkeypatch):
    _assert_persist_escape_sets_stop_event(
        monkeypatch, "_evaluate_single_rag",
        lambda ex: ex.execute_batch_rag([{"_orig_idx": 0}], SimpleNamespace()),
    )


def test_execute_batch_latency_soft_timeout_from_persist_sets_stop_event(monkeypatch):
    _assert_persist_escape_sets_stop_event(
        monkeypatch, "_evaluate_single_latency",
        lambda ex: ex.execute_batch_latency(
            [{"_orig_idx": 0}], SimpleNamespace(latency_config={})
        ),
    )


def test_persist_escape_stop_signal_reaches_queued_rows_before_shutdown(monkeypatch):
    """守卫必须先于 executor.__exit__ 置位（contextmanager 的 __exit__ 后进先出）

    置位若发生在 shutdown(wait=True) 之后，排队行早已等满 wait_timeout，
    守卫等于白加。这里让第二行阻塞在 stop_event 上复现该依赖。
    """
    stop_event = threading.Event()
    executor = EvaluationExecutor(
        max_workers=2, task_id="task-n1", stop_event=stop_event, execution_number=1
    )
    seen = []

    def _fake_single(payload, task):
        if payload.get("first"):
            return {"idx": payload["_orig_idx"], "success": True}
        seen.append(stop_event.wait(5))
        return {"idx": payload["_orig_idx"], "success": False, "error": "任务已停止"}

    calls = []

    def _persist(pos, payload, result):
        calls.append(pos)
        if payload.get("first"):
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(executor, "_evaluate_single", _fake_single)
    monkeypatch.setattr(executor, "_persist_row_result", _persist)
    monkeypatch.setattr(executor, "_update_progress", lambda *a, **k: None)

    started = time.monotonic()
    with pytest.raises(SoftTimeLimitExceeded):
        executor.execute_batch(
            [{"_orig_idx": 0, "first": True}, {"_orig_idx": 1}], SimpleNamespace()
        )
    elapsed = time.monotonic() - started

    assert calls == [0], "第一行落库时就该抛出"
    assert seen == [True], "排队行没收到停止信号：守卫置位晚于 shutdown(wait=True)"
    assert elapsed < 5, f"收尾被排队行拖住（{elapsed:.2f}s）"


def test_stop_event_set_failure_does_not_replace_soft_timeout(monkeypatch):
    """set() 抛 ConnectionError 时仍必须以 SoftTimeLimitExceeded 上抛

    否则任务失败标签从「软超时」变成「Redis 异常」，运维判断不出是评测跑太久
    还是中间件抖动。
    """
    stop_event = _BrokenStopEvent()
    executor = EvaluationExecutor(
        max_workers=1, task_id="task-n1", stop_event=stop_event, execution_number=1
    )
    monkeypatch.setattr(
        executor, "_evaluate_single",
        lambda payload, task: {"idx": payload["_orig_idx"], "success": True},
    )
    monkeypatch.setattr(executor, "_update_progress", lambda *a, **k: None)

    def _boom(pos, payload, result):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(executor, "_persist_row_result", _boom)

    with pytest.raises(SoftTimeLimitExceeded):
        executor.execute_batch([{"_orig_idx": 0}], SimpleNamespace())


def test_signal_stop_swallows_set_failure():
    """_signal_stop 自身不抛（收尾路径上任何异常都会替换正在传播的软超时）"""
    executor = EvaluationExecutor(max_workers=1, stop_event=_BrokenStopEvent())

    executor._signal_stop()  # 不抛即通过


def test_signal_stop_is_idempotent():
    stop_event = threading.Event()
    executor = EvaluationExecutor(max_workers=1, stop_event=stop_event)

    executor._signal_stop()
    executor._signal_stop()

    assert stop_event.is_set()
