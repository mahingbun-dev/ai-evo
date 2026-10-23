"""客户端申请/释放/重入/fail-open 测试（脚本结果用桩替换）"""
import json
import threading
import time
import uuid

import pytest

from app.services.judge_pool import client as client_mod
from app.services.judge_pool.client import AcquireResult, JudgePoolClient, Lease
from app.services.judge_pool.context import bind_judge_context, clear_current_lease, current_lease
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.errors import JudgePoolAborted, JudgePoolTimeout
from app.services.judge_pool.redis_keys import lease_data_key, leases_key, peak_key, wait_key

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setattr(client_mod, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(client_mod, "get_heartbeat", lambda: _NoopHeartbeat())
    monkeypatch.setattr(client_mod, "load_config", lambda force=False: _cfg())


@pytest.fixture(autouse=True)
def _clean_thread_leases():
    """租约登记是 thread-local：用例失败时残留会污染后续用例，前后各清一次"""
    def _drain():
        for engine in Engine:
            lease = current_lease(engine)
            if lease is not None:
                clear_current_lease(engine, lease)

    _drain()
    yield
    _drain()


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


class _RecordingRedis:
    """只记录 zrem 的假 redis：用来证明等待者确实被移出 wait zset

    `object()` 假客户端下 `_drop_waiter` 抛的 AttributeError 会被吞掉，
    缺陷会静默通过，所以这里必须记录调用。
    """

    def __init__(self):
        self.zrem_calls = []

    def zrem(self, key, member):
        self.zrem_calls.append((key, member))
        return 1


def test_stop_signal_removes_waiter_from_wait_zset():
    class Stop:
        def is_set(self):
            return True

    fake = _RecordingRedis()
    c = StubClient([])
    c._redis = fake
    with pytest.raises(JudgePoolAborted):
        c.acquire(Engine.GRAPH, stop_checker=Stop())

    assert len(fake.zrem_calls) == 1
    key, waiter_id = fake.zrem_calls[0]
    assert key == wait_key(Engine.GRAPH)
    uuid.UUID(waiter_id)  # 出队的正是本次 acquire 生成的 waiter_id


def test_wait_timeout_removes_waiter_from_wait_zset(monkeypatch):
    fake = _RecordingRedis()
    c = StubClient([AcquireResult(False, 1, [], 0)])
    c._redis = fake
    monkeypatch.setattr(c, "_sleep", lambda s: None)
    with pytest.raises(JudgePoolTimeout):
        c.acquire(Engine.GRAPH, wait_timeout=0)

    assert len(fake.zrem_calls) == 1
    key, waiter_id = fake.zrem_calls[0]
    assert key == wait_key(Engine.GRAPH)
    assert waiter_id == c.calls[0][1]


def test_redis_error_drops_queued_waiter(monkeypatch):
    """上一轮已排队，这一轮抛异常：fail-open 出口也必须出队"""

    class Flaky(JudgePoolClient):
        def __init__(self, redis_client):
            super().__init__(redis_client=redis_client)
            self.rounds = 0
            self.queued_waiter_ids = []

        def _try_acquire(self, engine, lease_id, detail, cfg):
            self.rounds += 1
            if self.rounds == 1:
                self.queued_waiter_ids.append(lease_id)
                return AcquireResult(False, 1, [], 0)
            raise ConnectionError("redis down")

    fake = _RecordingRedis()
    c = Flaky(fake)
    monkeypatch.setattr(c, "_sleep", lambda s: None)
    lease = c.acquire(Engine.GRAPH)

    assert lease.unavailable is True
    assert len(fake.zrem_calls) == 1
    key, waiter_id = fake.zrem_calls[0]
    assert key == wait_key(Engine.GRAPH)
    assert waiter_id == c.queued_waiter_ids[0]


def test_renew_failure_clears_thread_local_lease():
    """续约返回 False（已被回收/强释放）后不能再被判为重入放行"""

    class LostRenew(StubClient):
        def _lua_renew(self, engine, lease_id, detail, cfg):
            return 0

    c = LostRenew([AcquireResult(True, 1, [], -1), AcquireResult(True, 2, [], -1)])
    lease = c.acquire(Engine.GRAPH)
    assert c.renew(lease) is False
    assert current_lease(Engine.GRAPH) is None

    again = c.acquire(Engine.GRAPH)
    assert again.reentrant is False
    assert again.lease_id != lease.lease_id
    assert len(c.calls) == 2
    c.release(again)


def test_renew_exception_clears_thread_local_lease():
    class BoomRenew(StubClient):
        def _lua_renew(self, engine, lease_id, detail, cfg):
            raise ConnectionError("redis down")

    c = BoomRenew([AcquireResult(True, 1, [], -1)])
    lease = c.acquire(Engine.GRAPH)
    assert c.renew(lease) is False
    assert current_lease(Engine.GRAPH) is None


def test_release_does_not_clear_other_threads_lease():
    """release 语义保持不变：reentrant 租约仍是 no-op，不动线程本地登记"""

    class LostRenew(StubClient):
        def _lua_renew(self, engine, lease_id, detail, cfg):
            return 1

    c = LostRenew([AcquireResult(True, 1, [], -1)])
    lease = c.acquire(Engine.GRAPH)
    reentrant = c.acquire(Engine.GRAPH)
    assert c.release(reentrant) is False
    assert current_lease(Engine.GRAPH) is lease
    c.release(lease)
    assert current_lease(Engine.GRAPH) is None


def test_lost_lease_is_not_treated_as_reentrant():
    """心跳在别的线程把租约标记 lost 后，持有线程必须重新申请额度"""
    c = StubClient([AcquireResult(True, 1, [], -1), AcquireResult(True, 2, [], -1)])
    first = c.acquire(Engine.GRAPH)
    first.lost = True  # 心跳线程置位，持有线程只读

    second = c.acquire(Engine.GRAPH)

    assert second.reentrant is False
    assert second.lost is False
    assert second.lease_id != first.lease_id
    assert len(c.calls) == 2
    assert current_lease(Engine.GRAPH) is second
    c.release(second)


class _FakeHeartbeatClient:
    def __init__(self, renew_ok):
        self.renew_ok = renew_ok

    def renew(self, lease):
        return self.renew_ok

    def release(self, lease):
        return True


def _heartbeat(monkeypatch, renew_ok):
    from app.services.judge_pool.heartbeat import JudgePoolHeartbeat

    events = []
    hb = JudgePoolHeartbeat(
        client=_FakeHeartbeatClient(renew_ok=renew_ok),
        event_sink=lambda event, lease, **kwargs: events.append(event),
    )
    monkeypatch.setattr(hb, "_ensure_started", lambda: None)  # 不起后台线程
    return hb, events


def test_heartbeat_marks_lease_lost_on_renew_failure(monkeypatch):
    hb, events = _heartbeat(monkeypatch, renew_ok=False)
    lease = Lease(Engine.GRAPH, "l-lost", 0)
    hb.register(lease, threading.current_thread())

    hb.run_once()

    assert lease.lost is True
    assert events == ["lease_lost"]


def test_heartbeat_keeps_lease_when_renew_succeeds(monkeypatch):
    hb, events = _heartbeat(monkeypatch, renew_ok=True)
    lease = Lease(Engine.GRAPH, "l-alive", 0)
    hb.register(lease, threading.current_thread())

    hb.run_once()

    assert lease.lost is False
    assert events == []


# ---------------------------------------------------------------------------
# snapshot 契约形状与报错脱敏（/status 端点的数据源）
# ---------------------------------------------------------------------------


class _SnapshotRedis:
    """只实现 snapshot 用到的 5 个命令"""

    def __init__(self, lease=None):
        self.lease = lease

    def zcard(self, key):
        return 1 if self.lease and key == leases_key(Engine.GRAPH) else 0

    def zrange(self, key, start, end):
        if self.lease and key == leases_key(Engine.GRAPH):
            return [self.lease["lease_id"]]
        return []

    def hmget(self, key, ids):
        if self.lease and key == lease_data_key(Engine.GRAPH):
            return [json.dumps(self.lease)]
        return []

    def zscore(self, key, member):
        return time.time() * 1000 + 60_000

    def get(self, key):
        return "5" if key == peak_key(Engine.GRAPH) else "0"


class _BrokenRedis:
    def zcard(self, key):
        raise RuntimeError(
            "redis://user:secret@redis-host:6379/0 连接被拒绝 " + "x" * 500
        )


def test_snapshot_contract_shape():
    """引擎五项指标与租约关键字段由真实 client 产出，改错键名即红"""
    lease = {"lease_id": "l-1", "engine": "graph", "host": "h1", "pid": 42,
             "row_idx": 7, "acquired_at": 0, "last_renew_at": 0}
    snap = JudgePoolClient(redis_client=_SnapshotRedis(lease=lease)).snapshot()

    assert snap["redis_available"] is True
    assert snap["degraded"] is False
    assert set(snap["engines"]) == {e.value for e in Engine}
    for engine_value, info in snap["engines"].items():
        assert set(info) == {"limit", "active", "available", "waiting", "peak_1m"}
    assert snap["engines"]["graph"]["limit"] == _cfg().graph_limit
    assert snap["engines"]["graph"]["peak_1m"] == 5

    item = snap["leases"][0]
    for key in ("lease_id", "engine", "host", "pid", "row_idx", "ttl_remaining_seconds"):
        assert key in item, f"租约项缺少 {key}"
    assert item["ttl_remaining_seconds"] >= 0


def test_snapshot_error_masked_with_type_and_truncated():
    snap = JudgePoolClient(redis_client=_BrokenRedis()).snapshot()

    assert snap["redis_available"] is False
    assert snap["degraded"] is True
    assert snap["error"].startswith("RuntimeError: ")
    message = snap["error"].split(": ", 1)[1]
    assert len(message) <= 200
