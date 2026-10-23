"""软超时收尾：三个批量入口都必须先置停止信号再上抛（I2）

为什么：`raise` 之前会先执行 `with ThreadPoolExecutor(...)` 的 `__exit__`，
即 `shutdown(wait=True)` —— 它会等所有排队行跑完，而这些行正卡在
`judge_pool.acquire` 里最长等 wait_timeout（900s）。软超时 6600s / 硬超时
7200s 只剩 600s 余量，等不完就撞硬 SIGKILL：进程被杀时等待者来不及 ZREM
自己，幽灵队首扩散到别的任务。置位 stop_event 后，池等待在 ≤2s 内以
JudgePoolAborted 退出，收尾可达。
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
