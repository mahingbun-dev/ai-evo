"""评测任务级互斥锁与 celery 入口拒绝重投单元测试（缺陷B4）

背景：celery 配置 task_acks_late=True + prefork，worker 崩溃（OOM/SIGKILL）后
未 ACK 的消息重投，同一评测任务可能在两个 worker 并发执行——任务级 hub 镜像
目录出现 rmtree vs hardlink 竞态。EvalTaskLock（SET NX EX）抢锁失败时
run_evaluation_task 直接拒绝本次执行（不排队重入）。

redis 全部 fake（get_redis_client 打桩，与 test_hub_import_job 的 patch
redis.from_url 手法同思路）；run_evaluation_async 打桩，不连真实 DB/模型。
"""
from unittest.mock import patch

import pytest

from app.services import redis_stop_signal
from app.services.redis_stop_signal import EVAL_TASK_LOCK_TTL, EvalTaskLock


class FakeRedis:
    """最小 redis 客户端假实现：set(nx)/get/delete/exists（字符串值）"""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None  # redis SET NX 未抢到的语义
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    def exists(self, key):
        return 1 if key in self.store else 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# EvalTaskLock 语义
# ---------------------------------------------------------------------------

class TestEvalTaskLock:
    def test_acquire_then_second_lock_rejected(self, fake_redis):
        """同 task_id 第二个锁实例抢锁失败（互斥拒绝，不排队）"""
        first = EvalTaskLock("task-1")
        assert first.acquire() is True
        assert EvalTaskLock("task-1").acquire() is False

    def test_different_task_ids_independent(self, fake_redis):
        """不同 task_id 的锁互不影响"""
        assert EvalTaskLock("task-a").acquire() is True
        assert EvalTaskLock("task-b").acquire() is True

    def test_release_allows_reacquire(self, fake_redis):
        """释放后可重新抢锁（任务正常结束 → 同任务可再次启动）"""
        lock = EvalTaskLock("task-1")
        assert lock.acquire() is True
        lock.release()
        assert EvalTaskLock("task-1").acquire() is True

    def test_lock_value_records_holder_and_ttl(self, fake_redis):
        """锁值写持锁 worker 标识（hostname:pid），键带 TTL（worker 崩溃由过期兜底）"""
        import os
        import socket

        lock = EvalTaskLock("task-1")
        assert lock.acquire() is True
        assert lock.holder() == f"{socket.gethostname()}:{os.getpid()}"
        assert fake_redis.store["eval_task_lock:task-1"] == lock.holder()

    def test_acquire_uses_set_nx_with_ex(self, fake_redis):
        """抢锁走 SET NX EX（崩溃残留锁由 TTL 过期，TTL 覆盖 celery 2h 任务时限）"""
        calls = {}

        def _set(key, value, nx=False, ex=None):
            calls.update(key=key, nx=nx, ex=ex)
            return FakeRedis.set(fake_redis, key, value, nx=nx, ex=ex)

        fake_redis.set = _set
        assert EvalTaskLock("task-ttl").acquire() is True
        assert calls["nx"] is True
        assert calls["ex"] == EVAL_TASK_LOCK_TTL
        assert EVAL_TASK_LOCK_TTL >= 7200  # ≥ celery task_time_limit（2 小时）


# ---------------------------------------------------------------------------
# celery 入口 run_evaluation_task 的拒绝与释放
# ---------------------------------------------------------------------------

def _invoke_run_evaluation_task(task_id):
    from app.tasks.celery_tasks import run_evaluation_task
    return run_evaluation_task(task_id)


class TestRunEvaluationTaskLockWiring:
    def test_duplicate_execution_rejected(self, fake_redis):
        """抢锁失败：直接拒绝重投执行，不调用评测主流程"""
        EvalTaskLock("task-race").acquire()  # 模拟另一 worker 已持锁
        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async") as mock_run:
            result = _invoke_run_evaluation_task("task-race")
        assert result["status"] == "duplicate_rejected"
        assert result["task_id"] == "task-race"
        mock_run.assert_not_called()

    def test_lock_released_after_success(self, fake_redis):
        """正常执行结束：互斥锁释放（同任务可再次启动）"""
        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async",
                   return_value={"status": "success", "task_id": "task-ok"}):
            result = _invoke_run_evaluation_task("task-ok")
        assert result["status"] == "success"
        assert "eval_task_lock:task-ok" not in fake_redis.store, "锁应在任务结束后释放"
        assert EvalTaskLock("task-ok").acquire() is True

    def test_lock_released_after_failure(self, fake_redis):
        """评测主流程抛异常：锁仍释放（finally），异常原样上抛"""
        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                _invoke_run_evaluation_task("task-err")
        assert "eval_task_lock:task-err" not in fake_redis.store, "异常路径也必须释放锁"
        assert EvalTaskLock("task-err").acquire() is True

    def test_redis_acquire_error_fails_open(self, fake_redis, monkeypatch):
        """redis 抖动（抢锁瞬间不可用、随后恢复）：放行执行不阻断评测（fail-open）"""
        calls = {"n": 0}

        def _flaky_client():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("redis 抖动（模拟抢锁瞬间不可用）")
            return fake_redis  # 随后恢复（停止信号清理/锁释放正常走）

        monkeypatch.setattr(redis_stop_signal, "get_redis_client", _flaky_client)
        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async",
                   return_value={"status": "success", "task_id": "task-open"}) as mock_run:
            result = _invoke_run_evaluation_task("task-open")
        assert result["status"] == "success"
        mock_run.assert_called_once()
