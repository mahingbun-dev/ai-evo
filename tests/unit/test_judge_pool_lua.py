"""Lua 脚本语义测试：需要真实 Redis（不可达 skip，不假绿）"""
import time
import uuid

import pytest
import redis as redis_lib

from app.core.config import get_settings
from app.services.judge_pool import lua, redis_keys as keys
from app.services.judge_pool.engine import Engine, coerce_engine

pytestmark = pytest.mark.integration


@pytest.fixture
def r():
    url = get_settings().REDIS_URL
    try:
        client = redis_lib.from_url(url, decode_responses=True)
        client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Redis 不可达: {e}")
    for e in Engine:
        client.delete(keys.leases_key(e), keys.lease_data_key(e), keys.wait_key(e))
    yield client
    for e in Engine:
        client.delete(keys.leases_key(e), keys.lease_data_key(e), keys.wait_key(e))
    client.close()


def _acquire(r, engine, lease_id, now_ms, ttl_ms=60_000, limit=1, detail="{}", waiter=None):
    return r.eval(
        lua.ACQUIRE_SCRIPT, 3,
        keys.leases_key(engine), keys.lease_data_key(engine), keys.wait_key(engine),
        now_ms, ttl_ms, limit, lease_id, waiter or lease_id, detail, now_ms - 900_000,
    )


def test_engine_coerce():
    assert coerce_engine("graph") is Engine.GRAPH
    assert coerce_engine(Engine.PLUGIN) is Engine.PLUGIN
    with pytest.raises(ValueError):
        coerce_engine("openai")


def test_acquire_and_limit(r):
    now = int(time.time() * 1000)
    res = _acquire(r, Engine.GRAPH, "a", now)
    assert res[0] == 1
    res2 = _acquire(r, Engine.GRAPH, "b", now)
    assert res2[0] == 0
    assert res2[3] == 0  # 排队位次


def test_fifo_waiter_blocks_latecomer(r):
    now = int(time.time() * 1000)
    r.zadd(keys.wait_key(Engine.GRAPH), {"first": now - 1000})
    _acquire(r, Engine.GRAPH, "a", now, limit=2, waiter="second")
    res = _acquire(r, Engine.GRAPH, "b", now, limit=2, waiter="second")
    assert res[0] == 0, "队首不是自己时不得抢占"


def test_expired_lease_cleaned_and_returned(r):
    now = int(time.time() * 1000)
    r.zadd(keys.leases_key(Engine.GRAPH), {"old": now - 1000})
    r.hset(keys.lease_data_key(Engine.GRAPH), "old", '{"lease_id":"old"}')
    res = _acquire(r, Engine.GRAPH, "new", now, limit=1)
    assert res[0] == 1
    assert '{"lease_id":"old"}' in res[2]


def test_release_idempotent(r):
    now = int(time.time() * 1000)
    _acquire(r, Engine.GRAPH, "a", now)
    first = r.eval(lua.RELEASE_SCRIPT, 2, keys.leases_key(Engine.GRAPH), keys.lease_data_key(Engine.GRAPH), "a")
    second = r.eval(lua.RELEASE_SCRIPT, 2, keys.leases_key(Engine.GRAPH), keys.lease_data_key(Engine.GRAPH), "a")
    assert first[0] == 1
    assert second[0] == 0


def test_renew_lost_returns_zero(r):
    now = int(time.time() * 1000)
    _acquire(r, Engine.GRAPH, "a", now)
    assert r.eval(lua.RENEW_SCRIPT, 2, keys.leases_key(Engine.GRAPH), keys.lease_data_key(Engine.GRAPH), "a", now + 60_000, "{}") == 1
    r.delete(keys.leases_key(Engine.GRAPH))
    assert r.eval(lua.RENEW_SCRIPT, 2, keys.leases_key(Engine.GRAPH), keys.lease_data_key(Engine.GRAPH), "a", now + 60_000, "{}") == 0


def test_prune_returns_expired_and_stale_waiters(r):
    now = int(time.time() * 1000)
    r.zadd(keys.leases_key(Engine.GRAPH), {"old": now - 1000})
    r.hset(keys.lease_data_key(Engine.GRAPH), "old", '{"lease_id":"old"}')
    r.zadd(keys.wait_key(Engine.GRAPH), {"stale": now - 900_000})
    res = r.eval(
        lua.PRUNE_SCRIPT, 3,
        keys.leases_key(Engine.GRAPH), keys.lease_data_key(Engine.GRAPH), keys.wait_key(Engine.GRAPH),
        now, now - 800_000,
    )
    assert res[0] == ['{"lease_id":"old"}']
    assert res[1] == 1
