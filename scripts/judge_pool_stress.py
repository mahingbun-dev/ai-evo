"""裁判 LLM 并发池压测：多进程抢占，验证全局上限不被突破。

用法：
    python scripts/judge_pool_stress.py --engine graph --limit 3 --workers 12 --seconds 10

结论按退出码表达：0=PASS，1=FAIL（CI/脚本可直接用 $? 判断）。
脚本会临时改写并发池配置；无论正常结束、异常还是 Ctrl-C，都会在 finally 中
把进入时的配置完整写回（Redis + DB 同源），不给开发者库留测试值。
"""
import argparse
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

# 以 `python scripts/xxx.py` 运行时 sys.path[0] 是 scripts/，必须补仓库根才能 import app
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _worker(engine: str, seconds: float, hold: float,
            peak, acquired, errors, wait_timeout: int) -> None:
    """一个独立进程：循环 申请 → 读全局快照 → 短暂持有 → 释放"""
    from app.services.judge_pool.client import JudgePoolClient

    client = JudgePoolClient()
    end = time.monotonic() + seconds
    seen_peak = 0
    count = 0
    while time.monotonic() < end:
        try:
            lease = client.acquire(engine, wait_timeout=wait_timeout)
        except Exception as e:  # noqa: BLE001 - 统计后终止本进程，由父进程判定 FAIL
            with errors.get_lock():
                errors.value += 1
            print(f"pid={os.getpid()} 申请失败: {type(e).__name__}: {e}", flush=True)
            break
        try:
            if lease.unavailable:
                with errors.get_lock():
                    errors.value += 1
                print(f"pid={os.getpid()} Redis 不可用，租约降级放行（fail-open）", flush=True)
                break
            # 用公开 snapshot() 读全局活跃数（内部 ZCARD，跨进程一致）
            active = client.snapshot()["engines"][engine]["active"]
            seen_peak = max(seen_peak, active)
            with peak.get_lock():
                if seen_peak > peak.value:
                    peak.value = seen_peak
            count += 1
            time.sleep(hold)
        finally:
            client.release(lease)
    with acquired.get_lock():
        acquired.value += count
    print(f"pid={os.getpid()} acquired={count} peak_seen={seen_peak}", flush=True)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("必须是 >= 1 的整数")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("必须是 > 0 的数值")
    return value


def main() -> int:
    from app.services.judge_pool.config import (
        invalidate_config_cache,
        load_config,
        save_config,
    )
    from app.services.judge_pool.engine import Engine
    from app.services.judge_pool.redis_keys import leases_key

    parser = argparse.ArgumentParser(description="裁判 LLM 并发池压测")
    parser.add_argument("--engine", default="graph", choices=[e.value for e in Engine])
    parser.add_argument("--limit", type=_positive_int, default=3, help="压测期间该引擎的并发上限")
    parser.add_argument("--workers", type=_positive_int, default=12, help="并发进程数")
    parser.add_argument("--seconds", type=_positive_float, default=10.0, help="每个进程持续压测秒数")
    parser.add_argument("--hold", type=_positive_float, default=0.05, help="每次拿到额度后的持有秒数")
    args = parser.parse_args()

    # 先探测 Redis：连不上时要明确 FAIL，不能因为 fail-open 拿到「零错误」的假绿
    try:
        from app.services.redis_stop_signal import get_redis_client

        redis = get_redis_client()
        redis.ping()
    except Exception as e:  # noqa: BLE001
        print(f"RESULT: FAIL redis_unreachable error={type(e).__name__}: {e}")
        return 1

    original = load_config(force=True)
    limit_field = f"{args.engine}_limit"
    print(
        f"并发池压测: engine={args.engine} limit={args.limit} workers={args.workers} "
        f"seconds={args.seconds} hold={args.hold}"
    )
    print(f"原配置备份: {limit_field}={getattr(original, limit_field)}（结束后写回）")

    ctx = mp.get_context("spawn")
    peak = ctx.Value("i", 0)
    acquired = ctx.Value("i", 0)
    errors = ctx.Value("i", 0)
    procs = []
    residual = 0
    run_error = False
    try:
        save_config({limit_field: args.limit, "wait_timeout_seconds": 60}, "stress-run")
        invalidate_config_cache()

        procs = [
            ctx.Process(
                target=_worker,
                args=(args.engine, args.seconds, args.hold, peak, acquired, errors, 60),
                name=f"judge-pool-stress-{i}",
            )
            for i in range(args.workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        from app.services.judge_pool.client import JudgePoolClient

        client = JudgePoolClient()
        client.prune_expired(args.engine)
        residual = client.snapshot()["engines"][args.engine]["active"]
    except Exception as e:  # noqa: BLE001 - 任何异常都要落到明确的 FAIL 结论上
        run_error = True
        print(f"压测执行异常: {type(e).__name__}: {e}")
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
        save_config(original.to_dict(), "stress-restore")
        invalidate_config_cache()
        print(f"已还原配置: {limit_field}={getattr(load_config(force=True), limit_field)}")

    failed_workers = [p.exitcode for p in procs if p.exitcode != 0]
    observed_peak = int(peak.value)
    total_acquired = int(acquired.value)
    total_errors = int(errors.value)

    print(
        f"汇总: 获取次数合计={total_acquired} 峰值活跃={observed_peak} "
        f"获取失败={total_errors} 异常退出进程={len(failed_workers)} "
        f"结束后残留活跃={residual} 剩余租约键={redis.zcard(leases_key(args.engine))}"
    )

    ok = (
        not run_error
        and observed_peak <= args.limit
        and total_errors == 0
        and not failed_workers
        and total_acquired > 0
        and residual == 0
    )
    if ok and observed_peak < args.limit:
        print(
            f"WARN: 峰值活跃 {observed_peak} 未打满上限 {args.limit}，"
            "本轮没有真正考验限额（可加大 --workers 或 --hold）"
        )
    print(
        f"RESULT: {'PASS' if ok else 'FAIL'} "
        f"observed_peak={observed_peak} configured_limit={args.limit}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
