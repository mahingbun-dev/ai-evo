"""客户端申请/释放/重入/fail-open 测试（脚本结果用桩替换）"""
import threading
import time

import pytest

from app.services.judge_pool import client as client_mod
from app.services.judge_pool.client import AcquireResult, JudgePoolClient, Lease
from app.services.judge_pool.context import bind_judge_context, current_lease
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.errors import JudgePoolAborted, JudgePoolTimeout

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setattr(client_mod, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(client_mod, "get_heartbeat", lambda: _NoopHeartbeat())
    monkeypatch.setattr(client_mod, "load_config", lambda force=False: _cfg())


class _NoopHeartbeat:
    def register(self, lease, thread):
        pass

    def unregister(self, lease_id):
        pass


def _cfg():
    from app.services.judge_pool.config import DEFAULTS

    return DEFAULTS


class StubClient(JudgePoolClient):
    def __init__(self, results):
        super().__init__(redis_client=object())
        self.results = list(results)
        self.calls = []
        self.released = []

    def _try_acquire(self, engine, lease_id, detail, cfg):
        self.calls.append((engine, lease_id))
        return self.results.pop(0)

    def _lua_release(self, engine, lease_id):
        self.released.append(lease_id)
        return 1

    def _lua_renew(self, engine, lease_id, detail, cfg):
        return 1


def test_acquire_success_and_release():
    c = StubClient([AcquireResult(True, 1, [], -1)])
    lease = c.acquire(Engine.GRAPH, meta={"call_site": "t"})
    assert lease.lease_id
    assert current_lease(Engine.GRAPH) is lease
    assert c.release(lease) is True
    assert current_lease(Engine.GRAPH) is None
    assert c.released == [lease.lease_id]


def test_reentrant_acquire_does_not_consume_second_slot():
    c = StubClient([AcquireResult(True, 1, [], -1)])
    first = c.acquire(Engine.GRAPH)
    second = c.acquire(Engine.GRAPH)
    assert second.reentrant is True
    assert second.lease_id == first.lease_id
    assert len(c.calls) == 1
    c.release(first)
    assert current_lease(Engine.GRAPH) is None


def test_wait_timeout(monkeypatch):
    c = StubClient([AcquireResult(False, 1, [], 0)])
    monkeypatch.setattr(c, "_sleep", lambda s: None)
    with pytest.raises(JudgePoolTimeout):
        c.acquire(Engine.GRAPH, wait_timeout=0)


def test_stop_signal_aborts(monkeypatch):
    class Stop:
        def is_set(self):
            return True

    c = StubClient([])
    with pytest.raises(JudgePoolAborted):
        c.acquire(Engine.GRAPH, stop_checker=Stop())


def test_redis_error_fail_open(monkeypatch):
    class Boom(JudgePoolClient):
        def _try_acquire(self, *a, **k):
            raise ConnectionError("redis down")

    c = Boom(redis_client=object())
    lease = c.acquire(Engine.GRAPH)
    assert lease.unavailable is True
    assert c.release(lease) is False


def test_context_binding():
    with bind_judge_context(eval_task_id="t1", row_idx=3):
        from app.services.judge_pool.context import current_judge_context

        ctx = current_judge_context()
        assert ctx.eval_task_id == "t1"
        assert ctx.row_idx == 3


def test_context_propagates_to_child_thread():
    from app.services.judge_pool.context import capture_context, current_judge_context, run_in_context

    seen = {}

    def child():
        seen["task"] = current_judge_context().eval_task_id

    with bind_judge_context(eval_task_id="t1"):
        t = threading.Thread(target=run_in_context, args=(capture_context(), child))
        t.start()
        t.join()
    assert seen["task"] == "t1"


def test_child_thread_does_not_inherit_lease():
    """租约用 thread-local：子线程必须自己申请额度，不能被判成「重入」"""
    c = StubClient([AcquireResult(True, 1, [], -1), AcquireResult(True, 2, [], -1)])
    parent_lease = c.acquire(Engine.GRAPH)
    seen = {}

    def child():
        seen["lease"] = current_lease(Engine.GRAPH)
        seen["reentrant"] = c.acquire(Engine.GRAPH).reentrant

    t = threading.Thread(target=child)
    t.start()
    t.join()

    assert seen["lease"] is None
    assert seen["reentrant"] is False
    assert len(c.calls) == 2
    assert current_lease(Engine.GRAPH) is parent_lease
    assert c.release(parent_lease) is True
