"""审计队列与僵尸扫描行为测试（假 DB / 假 Redis，无真实外部依赖）

写线程由用例自己拉起、用完显式停止：不使用模块里常驻的真实写线程，
避免线程跨用例泄漏；`_writer_loop` 本身仍是真实实现，flush 屏障语义
因此是真验证而不是桩验证。
"""
import contextlib
import queue
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models.judge_pool_models import JudgePoolAuditLog
from app.services.judge_pool import audit as audit_mod
from app.services.judge_pool import scanner as scan_mod
from app.services.judge_pool.config import DEFAULTS
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.redis_keys import leases_key, peak_key, scan_lock_key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 审计：假 DB / 受控写线程
# ---------------------------------------------------------------------------


class _StopLoop(SystemExit):
    """停止受控写线程：_writer_loop 不吞 SystemExit，线程静默退出"""


class _StoppableQueue(queue.Queue):
    def __init__(self, maxsize=0):
        super().__init__(maxsize=maxsize)
        self._stop = False

    def stop(self):
        self._stop = True

    def get(self, block=True, timeout=None):
        if self._stop:
            raise _StopLoop()
        return super().get(block=block, timeout=timeout)


class _RecordingSession:
    """只有 commit 成功才把 add 的行「落库」，贴近真实事务语义"""

    def __init__(self, store, *, fail_commit=False):
        self.store = store
        self.fail_commit = fail_commit
        self._pending = []
        self.commits = 0
        self.rolled_back = 0
        self.closed = 0

    def add(self, row):
        self._pending.append(row)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.store.extend(self._pending)
        self._pending = []
        self.commits += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _fresh_audit_globals(monkeypatch):
    """队列/丢弃计数/写线程标志是模块级全局，逐用例隔离"""
    monkeypatch.setattr(audit_mod, "_QUEUE", queue.Queue(maxsize=10000))
    monkeypatch.setattr(audit_mod, "_DROPPED", [0])
    monkeypatch.setattr(audit_mod, "_writer_started", False)


@contextlib.contextmanager
def _driven_writer(monkeypatch):
    """拉起真实 _writer_loop 读测试队列，退出时停线程并断言已退出"""
    q = _StoppableQueue()
    monkeypatch.setattr(audit_mod, "_QUEUE", q)
    monkeypatch.setattr(audit_mod, "_ensure_writer", lambda: None)  # 不起模块常驻线程

    # 停止信号是测试自己的 SystemExit 子类：只屏蔽它，其余线程异常照常上报
    outer_hook = threading.excepthook

    def _hook(args):
        if args.exc_type is _StopLoop:
            return
        outer_hook(args)

    thread = threading.Thread(
        target=audit_mod._writer_loop, name="test-judge-pool-audit", daemon=True
    )
    threading.excepthook = _hook
    thread.start()
    try:
        yield thread
    finally:
        q.stop()
        try:
            thread.join(timeout=3)
        finally:
            threading.excepthook = outer_hook
        assert not thread.is_alive(), "审计写线程未能在用例结束前退出"


def test_record_then_flush_persists_all_queued_events(monkeypatch):
    rows = []
    monkeypatch.setattr(audit_mod, "SessionLocal", lambda: _RecordingSession(rows))

    with _driven_writer(monkeypatch):
        for idx in range(3):
            audit_mod.record_event("acquire", engine="graph", lease_id=f"l{idx}", row_idx=idx)
        audit_mod.flush_audit(timeout=2)

        # 屏障返回时队列必须已排空：少一条就说明 flush 提前返回
        assert len(rows) == 3
        assert [r.event for r in rows] == ["acquire", "acquire", "acquire"]
        assert [r.engine for r in rows] == ["graph", "graph", "graph"]
        assert [r.lease_id for r in rows] == ["l0", "l1", "l2"]
        assert all(isinstance(r, JudgePoolAuditLog) for r in rows)

        # 队列已空时再次 flush 立即返回，不产生重复行
        audit_mod.flush_audit(timeout=2)
        assert len(rows) == 3


def test_full_queue_drops_and_counts_instead_of_blocking(monkeypatch):
    q = queue.Queue(maxsize=1)
    q.put_nowait({"occupant": True})
    monkeypatch.setattr(audit_mod, "_QUEUE", q)
    monkeypatch.setattr(audit_mod, "_DROPPED", [0])
    monkeypatch.setattr(audit_mod, "_ensure_writer", lambda: None)

    # 不抛异常、不阻塞：丢弃并计数
    audit_mod.record_event("acquire", engine="graph")

    assert audit_mod._DROPPED[0] == 1
    assert q.qsize() == 1


def test_writer_survives_session_creation_failure(monkeypatch):
    """SessionLocal() 本身抛异常（原先在 try 之外）不能杀死写线程"""
    rows = []
    created = []

    def _session_local():
        created.append(1)
        if len(created) == 1:
            raise RuntimeError("db unavailable")
        return _RecordingSession(rows)

    monkeypatch.setattr(audit_mod, "SessionLocal", _session_local)

    with _driven_writer(monkeypatch) as thread:
        audit_mod.record_event("acquire", engine="graph", lease_id="dropped")
        audit_mod.flush_audit(timeout=1)

        assert thread.is_alive(), "单批失败后写线程不应退出"
        assert rows == [], "失败批次的记录应被丢弃"

        audit_mod.record_event("release", engine="graph", lease_id="landed")
        audit_mod.flush_audit(timeout=2)

        assert [(r.event, r.lease_id) for r in rows] == [("release", "landed")]


def test_writer_survives_commit_failure(monkeypatch):
    """commit 失败 → 回滚、丢弃该批，后续批次仍能落库"""
    # norm-exempt: evidence - 假 Session 会话，不连真实 DB，无需 SQL 证据
    rows = []
    sessions = []

    def _session_local():
        session = _RecordingSession(rows, fail_commit=not sessions)
        sessions.append(session)
        return session

    monkeypatch.setattr(audit_mod, "SessionLocal", _session_local)

    with _driven_writer(monkeypatch) as thread:
        audit_mod.record_event("acquire", engine="graph", lease_id="dropped")
        audit_mod.flush_audit(timeout=1)

        assert thread.is_alive()
        assert rows == []
        assert sessions[0].rolled_back == 1
        assert sessions[0].closed == 1

        audit_mod.record_event("release", engine="graph", lease_id="landed")
        audit_mod.flush_audit(timeout=2)

        # 失败批次没有被重试成重复行
        assert [(r.event, r.lease_id) for r in rows] == [("release", "landed")]
        assert sessions[1].commits == 1


# ---------------------------------------------------------------------------
# 审计查询 / 清理：假 Query（断言构造出的过滤条件与分页参数本身）
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows, total):
        self._rows = rows
        self._total = total
        self.filters = []
        self.offset_value = 0
        self.limit_value = None
        self.order = None

    def filter(self, expr):
        self.filters.append(expr)
        return self

    def count(self):
        return self._total

    def order_by(self, expr):
        self.order = expr
        return self

    def offset(self, value):
        self.offset_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        end = None if self.limit_value is None else self.offset_value + self.limit_value
        return self._rows[self.offset_value:end]


class _FakeQueryDB:
    def __init__(self, query):
        self.query_result = query
        self.queried_models = []

    def query(self, model):
        self.queried_models.append(model)
        return self.query_result


def _filter_pairs(query):
    """把 SQLAlchemy 过滤表达式还原成 (列名, 值)，直接断言过滤条件本身"""
    return [(expr.left.key, expr.right.value) for expr in query.filters]


def test_query_audit_filters_and_paginates():
    # norm-exempt: evidence - 假 Query 对象，断言的是条件构造而非真实落库
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 2, tzinfo=timezone.utc)
    rows = [object() for _ in range(5)]
    query = _FakeQuery(rows, total=9)
    db = _FakeQueryDB(query)

    result_rows, total = audit_mod.query_audit(
        db, engine="graph", event="acquire", start=start, end=end, skip=2, limit=2
    )

    assert db.queried_models == [JudgePoolAuditLog]
    assert total == 9
    assert result_rows == rows[2:4]
    assert _filter_pairs(query) == [
        ("engine", "graph"),
        ("event", "acquire"),
        ("created_at", start),
        ("created_at", end),
    ]
    assert query.offset_value == 2
    assert query.limit_value == 2
    assert "DESC" in str(query.order)


def test_query_audit_defaults_skip_filters():
    # norm-exempt: evidence - 假 Query 对象，不连真实 DB
    query = _FakeQuery([], total=0)
    db = _FakeQueryDB(query)

    rows, total = audit_mod.query_audit(db)

    assert rows == []
    assert total == 0
    assert query.filters == []
    assert query.offset_value == 0
    assert query.limit_value == 50


class _FakeDeleteQuery:
    def __init__(self, deleted):
        self.deleted = deleted
        self.filters = []

    def filter(self, expr):
        self.filters.append(expr)
        return self

    def delete(self):
        return self.deleted


class _FakeCleanupDB:
    def __init__(self, deleted):
        self.deleted = deleted
        self.query_obj = None
        self.commits = 0

    def query(self, model):
        assert model is JudgePoolAuditLog
        self.query_obj = _FakeDeleteQuery(self.deleted)
        return self.query_obj

    def commit(self):
        self.commits += 1


def test_cleanup_old_audit_deletes_rows_older_than_retention():
    # norm-exempt: evidence - 假 DB 会话，断言删除条件与返回值
    db = _FakeCleanupDB(deleted=7)
    before = datetime.now(timezone.utc) - timedelta(days=30)
    count = audit_mod.cleanup_old_audit(db, retention_days=30)
    after = datetime.now(timezone.utc) - timedelta(days=30)

    assert count == 7
    assert db.commits == 1  # 删除必须提交

    pairs = _filter_pairs(db.query_obj)
    assert [key for key, _ in pairs] == ["created_at"]
    cutoff = pairs[0][1]
    assert cutoff.tzinfo is not None
    # cutoff 应是 now - 30d（允许执行耗时带来的误差）
    assert before - timedelta(seconds=5) <= cutoff <= after + timedelta(seconds=5)


def test_cleanup_old_audit_nothing_deleted_returns_zero():
    # norm-exempt: evidence - 假 DB 会话，不连真实 DB
    db = _FakeCleanupDB(deleted=None)

    assert audit_mod.cleanup_old_audit(db, retention_days=1) == 0
    assert db.commits == 1


# ---------------------------------------------------------------------------
# 僵尸扫描：假 Redis / 假客户端
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, *, lock_ok=True, active=3):
        self.lock_ok = lock_ok
        self.active = active
        self.set_calls = []
        self.zcard_calls = []
        self.eval_calls = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        return self.lock_ok

    def zcard(self, key):
        self.zcard_calls.append(key)
        return self.active

    def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, args))
        return 1


class _FakeClient:
    def __init__(self):
        self.pruned = []

    def prune_expired(self, engine):
        self.pruned.append(engine.value)
        return []


class _FakeSessionCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _scanner(monkeypatch, redis, *, client=None, cfg=None):
    scanner = scan_mod.JudgePoolScanner(client=client or _FakeClient())
    monkeypatch.setattr(scanner, "_redis", lambda: redis)
    monkeypatch.setattr(scan_mod, "load_config", lambda: cfg or DEFAULTS)
    return scanner


def _lock_sets(redis):
    return [call for call in redis.set_calls if call[2]]  # nx=True 只用于扫描锁


def test_run_once_skips_when_lock_not_acquired(monkeypatch):
    redis = _FakeRedis(lock_ok=False)
    client = _FakeClient()
    scanner = _scanner(monkeypatch, redis, client=client)

    scanner.run_once()

    assert client.pruned == []
    assert redis.zcard_calls == []
    assert len(redis.set_calls) == 1  # 只尝试了加锁，未写峰值
    key, _token, nx, ex = redis.set_calls[0]
    assert (key, nx, ex) == (scan_lock_key(), True, scan_mod._LOCK_TTL_SECONDS)
    assert redis.eval_calls == []  # 没拿到锁就不能释放别人的锁


def test_run_once_returns_when_lock_acquisition_errors(monkeypatch):
    class _BrokenRedis(_FakeRedis):
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    redis = _BrokenRedis()
    client = _FakeClient()
    scanner = _scanner(monkeypatch, redis, client=client)

    scanner.run_once()  # 不抛异常

    assert client.pruned == []
    assert redis.eval_calls == []


def test_run_once_sweeps_all_engines_and_refreshes_peak(monkeypatch):
    redis = _FakeRedis(active=5)
    client = _FakeClient()
    cfg = replace(DEFAULTS, audit_retention_days=7)
    scanner = _scanner(monkeypatch, redis, client=client, cfg=cfg)

    cleanup_calls = []
    monkeypatch.setattr(
        audit_mod, "cleanup_old_audit", lambda db, days: cleanup_calls.append(days) or 0
    )
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSessionCtx())

    scanner.run_once()

    assert client.pruned == [engine.value for engine in Engine]
    assert redis.zcard_calls == [leases_key(engine) for engine in Engine]

    peaks = {key: (value, ex) for key, value, nx, ex in redis.set_calls if not nx}
    assert peaks == {peak_key(engine): ("5", 120) for engine in Engine}

    # token 加锁、按 token 比较删除释放
    lock_sets = _lock_sets(redis)
    assert len(lock_sets) == 1
    _, token, _, _ = lock_sets[0]
    assert len(redis.eval_calls) == 1
    script, numkeys, args = redis.eval_calls[0]
    assert "redis.call('GET', KEYS[1]) == ARGV[1]" in script
    assert "redis.call('DEL', KEYS[1])" in script
    assert numkeys == 1
    assert args == (scan_lock_key(), token)

    # 清理参数来自配置而非硬编码
    assert cleanup_calls == [7]


def test_run_once_releases_lock_when_sweep_fails(monkeypatch):
    class _BrokenClient:
        def prune_expired(self, engine):
            raise RuntimeError("sweep failed")

    redis = _FakeRedis()
    scanner = _scanner(monkeypatch, redis, client=_BrokenClient())

    with pytest.raises(RuntimeError):
        scanner.run_once()

    # finally 里仍按 token 释放锁
    assert len(redis.eval_calls) == 1
    _, _, args = redis.eval_calls[0]
    assert args[0] == scan_lock_key()
    assert args[1] == _lock_sets(redis)[0][1]
