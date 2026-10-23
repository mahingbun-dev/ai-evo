"""Lua 脚本语义测试：需要真实 Redis（不可达 skip，不假绿）"""
import time
import uuid

import pytest
import redis as redis_lib

from app.core.config import get_settings
from app.services.judge_pool import lua, redis_keys as keys
from app.services.judge_pool.engine import Engine, coerce_engine

pytestmark = pytest.mark.integration

# 本文件创建的存活凭证 key（teardown 统一删净，不在共享 Redis 里留垃圾）
_ALIVE_KEYS: list[str] = []


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
    if _ALIVE_KEYS:
        client.delete(*_ALIVE_KEYS)
        _ALIVE_KEYS.clear()
    client.close()


def _mark_alive(r, engine, waiter: str, ex: int = 60) -> None:
    """给等待者挂一张存活凭证（等价于 client._try_acquire 每轮排队时的 SET）"""
    key = keys.wait_alive_key(engine, waiter)
    r.set(key, "1", ex=ex)
    _ALIVE_KEYS.append(key)


def _acquire(r, engine, lease_id, now_ms, ttl_ms=60_000, limit=1, detail="{}", waiter=None,
             alive=True):
    waiter = waiter or lease_id
    if alive:
        _mark_alive(r, engine, waiter)
    return r.eval(
        lua.ACQUIRE_SCRIPT, 3,
        keys.leases_key(engine), keys.lease_data_key(engine), keys.wait_key(engine),
        now_ms, ttl_ms, limit, lease_id, waiter, detail, now_ms - 900_000,
        keys.wait_alive_prefix(engine),
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
    _mark_alive(r, Engine.GRAPH, "first")  # 存活队首才有权挡人（幽灵队首见下）
    _acquire(r, Engine.GRAPH, "a", now, limit=2, waiter="second")
    res = _acquire(r, Engine.GRAPH, "b", now, limit=2, waiter="second")
    assert res[0] == 0, "队首不是自己时不得抢占"


def test_dead_waiter_head_does_not_block_acquire(r):
    """幽灵队首（存活凭证缺失）必须被回收：否则 active=0 也拿不到额度，
    所有后来者被 am_head=false 挡到 wait_timeout（900s）过期"""
    now = int(time.time() * 1000)
    r.zadd(keys.wait_key(Engine.GRAPH), {"ghost": now - 1000})

    res = _acquire(r, Engine.GRAPH, "a", now, limit=1)

    assert res[0] == 1, "有空闲额度却被幽灵队首挡住"
    assert r.zscore(keys.wait_key(Engine.GRAPH), "ghost") is None, "幽灵队首未出队"


def test_dead_waiters_pruned_only_up_to_live_head(r):
    """连续幽灵都要清掉，但遇到存活队首必须停下（FIFO 公平性不能被破坏）"""
    now = int(time.time() * 1000)
    r.zadd(keys.wait_key(Engine.GRAPH), {
        "ghost1": now - 3000, "ghost2": now - 2000, "live": now - 1000,
    })
    _mark_alive(r, Engine.GRAPH, "live")

    res = _acquire(r, Engine.GRAPH, "a", now, limit=1)

    assert res[0] == 0, "存活队首仍在时不得抢占"
    wait = keys.wait_key(Engine.GRAPH)
    assert r.zscore(wait, "ghost1") is None and r.zscore(wait, "ghost2") is None
    assert r.zscore(wait, "live") is not None, "存活等待者被误删"


def test_acquire_deletes_own_alive_credential(r):
    """拿到租约后自己的存活凭证必须删除，不留残留 key"""
    now = int(time.time() * 1000)
    res = _acquire(r, Engine.GRAPH, "a", now, limit=1)

    assert res[0] == 1
    assert r.exists(keys.wait_alive_key(Engine.GRAPH, "a")) == 0


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
