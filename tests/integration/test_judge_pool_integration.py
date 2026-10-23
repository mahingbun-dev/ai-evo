"""真实 Redis + 多线程/多进程验证并发池全局上限与僵尸租约回收。

口径（见 tests/integration/conftest.py）：跑不了明确 skip，不许假绿。

副作用纪律：
- 本文件会临时改写并发池配置（graph 限额/租约 TTL），每个用例结束前用
  save_config 完整还原到进入文件前的取值（Redis + DB 同源），并清缓存；
- Redis 里只清本进程/本文件创建的租约键，绝不清 `judge_pool:conf`；
- 并发池非空闲（有其他进程的真实租约）时明确 skip —— 此时既不能保证
  断言口径，也不该为了跑测试去抢真实评测的额度。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import redis as redis_lib

from app.core.config import get_settings
from app.services.judge_pool import client as client_mod
from app.services.judge_pool.client import JudgePoolClient
from app.services.judge_pool.config import invalidate_config_cache, load_config, save_config
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.redis_keys import lease_data_key, leases_key, wait_key

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE = Engine.GRAPH
_TEST_LEASE_SOURCE = "integration-test"


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def redis_client():
    """被测代码实际连接的 Redis；不可达时明确 skip（不假绿）"""
    url = get_settings().REDIS_URL
    try:
        c = redis_lib.from_url(url, decode_responses=True)
        c.ping()
    except Exception as e:  # noqa: BLE001 - 探测失败统一 skip
        pytest.skip(f"Redis {url} 不可达（{e}），并发池集成用例跳过（跑不了明确 skip）")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def restore_pool_config(redis_client):
    """用例前抓当前配置，用例后（含失败/异常）原样写回。

    save_config 是全量覆盖写：用它写回 to_dict() 即可同时还原 Redis 热源与
    DB 持久化，不会把开发者库里的运维配置改成测试值。
    """
    original = load_config(force=True)
    try:
        yield original
    finally:
        save_config(original.to_dict(), "test-restore")
        invalidate_config_cache()


def _detail_json(lease_id: str, *, pid: int = -1) -> str:
    """租约明细的最小合法形状（client.acquire 写进 lease_data 的字段子集）

    meta.source 是本文件创建租约的标记：注入租约的 pid 不可靠（-1 会被
    os.kill(-1, 0) 判成「存活」），清理一律认标记、不认 pid。
    """
    now = int(time.time() * 1000)
    return json.dumps(
        {
            "lease_id": lease_id,
            "engine": ENGINE.value,
            "host": socket.gethostname(),
            "pid": pid,
            "thread": "injected",
            "eval_task_id": None,
            "celery_task_id": None,
            "row_idx": None,
            "acquired_at": now,
            "last_renew_at": now,
            "meta": {"source": _TEST_LEASE_SOURCE},
        },
        ensure_ascii=False,
    )


def _purge_test_leases(client) -> int:
    """删除本文件创建的租约（认 meta.source 标记，进入/退出两侧都调用）。

    不能只靠 pid 判活：注入租约的 pid=-1 调 os.kill(-1, 0) 不抛
    ProcessLookupError，会被当成活进程而漏清；漏清又会让下一个用例因
    「池非空闲」skip —— CI 会把 skip 当成没跑而放过。
    """
    removed = 0
    for raw in client.hvals(lease_data_key(ENGINE)):
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (item.get("meta") or {}).get("source") != _TEST_LEASE_SOURCE:
            continue
        lease_id = item.get("lease_id")
        if not lease_id:
            continue
        client.zrem(leases_key(ENGINE), lease_id)
        client.hdel(lease_data_key(ENGINE), lease_id)
        client.zrem(wait_key(ENGINE), lease_id)  # 排队者 id 与租约 id 同值
        removed += 1
    return removed


def _inject_expired_lease(client, lease_id: str | None = None) -> str:
    """直接写一条「分数已过期」的租约 —— 等价于死进程租约过了 TTL 的状态"""
    lease_id = lease_id or f"dead-{uuid.uuid4()}"
    client.zadd(leases_key(ENGINE), {lease_id: int(time.time() * 1000) - 1000})
    client.hset(lease_data_key(ENGINE), lease_id, _detail_json(lease_id))
    return lease_id


def _release_dead_leftovers(client) -> None:
    """清掉本机已退出进程留下的租约。

    上一次运行崩溃会留下僵尸租约，不先清会让本文件后续每次运行都因「池非
    空闲」skip；只按 pid 判活，活着的进程（真实评测）绝不碰。
    """
    for raw in client.hvals(lease_data_key(ENGINE)):
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        pid = item.get("pid")
        # pid<=1 绝不能 os.kill：-1 表示「所有进程」，探测不报错会被误判为存活；
        # 这类注入租约由 _purge_test_leases 按标记清理
        if item.get("host") != socket.gethostname() or not isinstance(pid, int) or pid <= 1:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            client.zrem(leases_key(ENGINE), item["lease_id"])
            client.hdel(lease_data_key(ENGINE), item["lease_id"])
        except OSError:
            # 无权限探测视为存活，宁可 skip 也不误删
            continue


@pytest.fixture()
def idle_pool(redis_client):
    """确保 graph 引擎此刻没有别的进程在用；进入/退出两侧都清本文件创建的租约"""
    client = JudgePoolClient(redis_client=redis_client)
    # 进入侧三道清理缺一不可：先按标记清本文件的历史残留（含 pid 不可判活的
    # 注入租约），再走扫描回收真实过期租约，最后清本机死进程遗留
    _purge_test_leases(redis_client)
    client.prune_expired(ENGINE)
    _release_dead_leftovers(redis_client)
    _purge_test_leases(redis_client)

    snap = client.snapshot()
    active = snap["engines"][ENGINE.value]["active"]
    if active:
        pytest.skip(
            f"graph 并发池当前非空闲（活跃租约 {active} 个），"
            "为避免抢占真实评测额度与破坏峰值断言跳过"
        )
    # 空闲时排队者不可能合法存在（有额度会立刻被拿掉），清掉上轮崩溃残留的
    # 队首会让后续所有申请被 am_head 判定挡住，直到 wait_timeout 过期
    redis_client.delete(wait_key(ENGINE))

    yield

    _purge_test_leases(redis_client)
    _release_dead_leftovers(redis_client)
    # 本进程内已释放但 Redis 调用失败的租约兜底：只删明细里 pid 是自己的
    for raw in redis_client.hvals(lease_data_key(ENGINE)):
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if item.get("pid") == os.getpid():
            redis_client.zrem(leases_key(ENGINE), item["lease_id"])
            redis_client.hdel(lease_data_key(ENGINE), item["lease_id"])
            redis_client.zrem(wait_key(ENGINE), item["lease_id"])
    # 清理必须彻底：残留会让下一个用例静默 skip，而 skip 在 CI 里等于没跑
    assert redis_client.zcard(leases_key(ENGINE)) == 0, (
        f"用例结束后仍有活跃租约: {redis_client.hvals(lease_data_key(ENGINE))}"
    )
    assert redis_client.zcard(wait_key(ENGINE)) == 0, "用例结束后等待队列仍有成员"


def _apply(**overrides) -> None:
    save_config(overrides, "test-integration")
    invalidate_config_cache()


# ---------------------------------------------------------------- 线程级上限


def test_global_limit_holds_across_threads(redis_client, idle_pool):
    """9 个线程抢 3 个额度：全局活跃数必须恰好被压在 3（不是 <=3 的空转通过）。"""
    limit = 3
    _apply(graph_limit=limit, wait_timeout_seconds=30)

    # 用 Barrier 而不是「加一/减一计数」制造同时持有：计数无论先减还是先
    # release，都有一侧窗口（先减=Redis 仍持有但不计入，先 release=计数虚高），
    # 判断超限时会误判。Barrier 只放行真正同时持有额度的线程，峰值一律以
    # Redis snapshot（唯一权威）为准。线程数必须是 limit 的整数倍，否则最后一
    # 组凑不齐会在超时后 BrokenBarrierError。
    barrier = threading.Barrier(limit, timeout=20)
    snapshots: list[tuple[int, int]] = []  # (active, available)，每个持有者各一条
    snap_lock = threading.Lock()
    errors: list[str] = []
    acquired = [0]

    def worker() -> None:
        client = JudgePoolClient()
        try:
            lease = client.acquire(ENGINE, wait_timeout=30)
            try:
                if lease.unavailable or lease.reentrant:
                    raise AssertionError(f"意外租约状态: {lease}")
                snap = client.snapshot()["engines"][ENGINE.value]
                with snap_lock:
                    snapshots.append((snap["active"], snap["available"]))
                # 三个持有者到齐才放行；到齐那一刻 Redis 里必然同时有 3 个租约
                barrier.wait()
                time.sleep(0.05)
                with snap_lock:
                    acquired[0] += 1
            finally:
                client.release(lease)
        except BaseException as e:  # noqa: BLE001 - 线程内异常要带回主线程断言
            errors.append(f"{type(e).__name__}: {e}")

    threads = [
        threading.Thread(target=worker, name=f"pool-it-{i}", daemon=True)
        for i in range(9)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)

    assert errors == [], f"工作线程异常: {errors}"
    assert all(not t.is_alive() for t in threads), "工作线程未在 40s 内结束"
    assert len(snapshots) == 9, f"只有 {len(snapshots)}/9 个线程拿到额度"
    assert acquired[0] == 9, f"只有 {acquired[0]}/9 个线程走完持有流程"

    peak = max(active for active, _ in snapshots)
    free_min = min(available for _, available in snapshots)
    # 上界 + 下界：既没超限，也证明限流真的生效（否则峰值会是 9）
    assert peak == limit, f"Redis 侧并发峰值 {peak} != {limit}（各持有者观测: {snapshots}）"
    assert free_min == 0, f"打满时 snapshot 仍显示有空闲额度: {snapshots}"

    after = JudgePoolClient(redis_client=redis_client).snapshot()["engines"][ENGINE.value]
    assert after["active"] == 0, f"用例结束后额度未归还干净: {after}"
    assert after["waiting"] == 0, f"用例结束后等待队列未清空: {after}"


# ---------------------------------------------------------------- 僵尸回收


def test_expired_lease_slot_reclaimed_by_acquire(redis_client, idle_pool):
    """过期租约不会永久占额度：下一个 acquire 会先回收再占位（无 sleep）。"""
    _apply(graph_limit=1, wait_timeout_seconds=5)
    dead_id = _inject_expired_lease(redis_client)

    before = JudgePoolClient(redis_client=redis_client).snapshot()["engines"][ENGINE.value]
    assert before["active"] == 1 and before["available"] == 0, (
        f"注入的过期租约未占到唯一额度: {before}"
    )

    client = JudgePoolClient()
    started = time.monotonic()
    lease = client.acquire(ENGINE, wait_timeout=5)
    try:
        assert lease.unavailable is False
        # 若回收失效，这里会在 wait_timeout=5 后抛 JudgePoolTimeout
        assert time.monotonic() - started < 4, "回收过期租约后仍然等待过久"
        assert redis_client.zscore(leases_key(ENGINE), dead_id) is None, "过期租约仍在 zset"
        assert not redis_client.hexists(lease_data_key(ENGINE), dead_id), "过期租约明细未删"
        snap = client.snapshot()["engines"][ENGINE.value]
        assert snap["active"] == 1 and snap["available"] == 0
    finally:
        client.release(lease)


def test_prune_expired_reports_and_frees_slot(redis_client, idle_pool):
    """扫描路径（scanner.run_once 即逐引擎调用 prune_expired）要报出过期租约。"""
    _apply(graph_limit=1, wait_timeout_seconds=5)
    dead_id = _inject_expired_lease(redis_client)

    client = JudgePoolClient()
    details = client.prune_expired(ENGINE)
    reported = []
    for raw in details:
        try:
            reported.append(json.loads(raw).get("lease_id"))
        except (TypeError, ValueError):
            reported.append(None)

    assert dead_id in reported, f"prune_expired 未报告过期租约 {dead_id}: {details}"
    assert redis_client.zscore(leases_key(ENGINE), dead_id) is None
    assert not redis_client.hexists(lease_data_key(ENGINE), dead_id)

    lease = client.acquire(ENGINE, wait_timeout=5)
    try:
        assert lease.unavailable is False
    finally:
        client.release(lease)


# ---------------------------------------------------------------- 真实 SIGKILL

_ACQUIRE_AND_DIE_SCRIPT = """
import json, os, sys
from app.services.judge_pool.client import JudgePoolClient

lease = JudgePoolClient().acquire("graph", wait_timeout=15)
print(json.dumps({"lease_id": lease.lease_id, "unavailable": lease.unavailable}))
sys.stdout.flush()
os._exit(0)  # 模拟 SIGKILL：不 release、不跑 finally/atexit、不再续约
"""


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def test_sigkill_process_lease_reclaimed_by_prune(redis_client, idle_pool):
    """真子进程拿租约后 os._exit：槽位由 TTL 过期 + 扫描回收，额度恢复可用。"""
    ttl = 3
    _apply(
        graph_limit=1,
        lease_ttl_seconds=ttl,
        heartbeat_interval_seconds=1,
        wait_timeout_seconds=10,
    )

    proc = subprocess.run(
        [sys.executable, "-c", _ACQUIRE_AND_DIE_SCRIPT],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert proc.returncode == 0, f"子进程失败: {proc.stderr[-800:]}"
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    lease_id = info["lease_id"]
    client = JudgePoolClient()
    try:
        assert info["unavailable"] is False, "子进程未真正拿到额度（Redis 不可用？）"
        # 死进程的额度仍在：这正是需要被回收的僵尸租约
        assert redis_client.zscore(leases_key(ENGINE), lease_id) is not None
        snapshot = client.snapshot()["engines"][ENGINE.value]
        assert snapshot["active"] == 1 and snapshot["available"] == 0

        deadline = time.monotonic() + ttl + 7
        reported = []
        while time.monotonic() < deadline:
            details = client.prune_expired(ENGINE)
            reported = []
            for raw in details:
                try:
                    reported.append(json.loads(raw).get("lease_id"))
                except (TypeError, ValueError):
                    reported.append(None)
            if lease_id in reported:
                break
            time.sleep(0.2)

        assert lease_id in reported, (
            f"租约 {lease_id} 在 {ttl}s TTL 后仍未被子进程退出后的扫描回收；"
            f"最近一次扫描明细: {reported}"
        )
        assert redis_client.zscore(leases_key(ENGINE), lease_id) is None
        assert not redis_client.hexists(lease_data_key(ENGINE), lease_id)

        lease = client.acquire(ENGINE, wait_timeout=5)  # 额度已恢复，无需等待 TTL
        try:
            assert lease.unavailable is False
        finally:
            client.release(lease)
    finally:
        client.force_release(ENGINE, lease_id)  # 断言失败时的兜底清理


# ---------------------------------------------------------------- 跨进程上限

_MP_WORKER_SCRIPT = """
import json, sys, time
from app.services.judge_pool.client import JudgePoolClient

engine, seconds, hold = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
client = JudgePoolClient()
end = time.monotonic() + seconds
peak = acquired = failed = 0
while time.monotonic() < end:
    try:
        lease = client.acquire(engine, wait_timeout=20)
    except Exception as e:
        failed += 1
        print("ERR " + type(e).__name__ + ": " + str(e), file=sys.stderr)
        break
    try:
        if lease.unavailable:
            failed += 1
            break
        active = client.snapshot()["engines"][engine]["active"]
        peak = max(peak, active)
        acquired += 1
        time.sleep(hold)
    finally:
        client.release(lease)
print(json.dumps({"peak": peak, "acquired": acquired, "failed": failed}))
"""


def test_global_limit_holds_across_processes(redis_client, idle_pool):
    """4 个独立进程 × 各自循环申请，2 个额度：全局峰值必须恰好为 2。

    如果限流是按进程算的，4 个进程各占 2 个额度，全局峰值会到 8 —— 这条断言
    直接盯住「跨进程共享同一份额度」这个卖点。
    """
    limit, workers, seconds = 2, 4, 2.0
    _apply(graph_limit=limit, wait_timeout_seconds=20)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _MP_WORKER_SCRIPT, ENGINE.value, str(seconds), "0.05"],
            cwd=str(REPO_ROOT),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(workers)
    ]
    results = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=seconds + 40)
            assert proc.returncode == 0, f"子进程异常退出: {err[-800:]}"
            results.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    assert all(r["failed"] == 0 for r in results), f"子进程内申请失败: {results}"
    assert sum(r["acquired"] for r in results) > 0, "没有任何一次成功申请，用例无意义"
    observed_peak = max(r["peak"] for r in results)  # snapshot 读的是全局 ZCARD
    assert observed_peak == limit, (
        f"跨进程全局峰值 {observed_peak} != 配置上限 {limit}（子进程明细: {results}）"
    )

    after = JudgePoolClient(redis_client=redis_client).snapshot()["engines"][ENGINE.value]
    assert after["active"] == 0 and after["waiting"] == 0, f"额度未归还干净: {after}"
