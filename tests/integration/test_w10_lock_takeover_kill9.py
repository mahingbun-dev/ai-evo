"""W10-2 EvalTaskLock 接管集成测试：模拟持锁进程 kill -9 后接管

整改计划 §3 W10 测试设计：「锁接管模拟 kill -9 持锁进程」。真实 Redis 上，
子进程以 hostname:pid 写入互斥锁后长睡，测试进程 SIGKILL 并收尸该子进程，
断言 EvalTaskLock 抢锁从「拒绝」变为「接管成功 + 短 TTL + 续期可用」。
"""
import os
import socket
import subprocess
import sys
import time

import pytest

from app.services import redis_stop_signal
from app.services.redis_stop_signal import (
    EVAL_TASK_LOCK_TAKEOVER_TTL,
    EvalTaskLock,
)

pytestmark = pytest.mark.integration

# 子进程源码：真实 Redis 写入互斥锁（hostname:pid），打印 LOCKED 后长睡模拟执行中。
# Redis 地址经 argv 注入（host/port 来自测试进程所用 local_redis 连接参数）——
# 不能硬编码端口：本地 dev 栈常把 Redis 映射到非 6379 端口（如 6380），
# 测试进程侧 monkeypatch 后连 settings.REDIS_URL 指向的实例，子进程必须写
# 同一实例，否则「存活时拒绝」断言查不到子进程的锁而假红。
_CHILD_SRC = """
import os, socket, sys, time
import redis
r = redis.Redis(host=sys.argv[2], port=int(sys.argv[3]), db=0, decode_responses=True)
r.set(sys.argv[1], f"{socket.gethostname()}:{os.getpid()}", nx=True, ex=3600)
print("LOCKED", flush=True)
time.sleep(120)
"""


def _wait_pid_gone(pid: int, timeout: float = 3.0) -> bool:
    """轮询确认 pid 已从进程表消失（收尸完成后应立即消失）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def app_lock_on_local_redis(local_redis, monkeypatch):
    """让 EvalTaskLock 使用本地真实 Redis（与子进程写入同一实例）"""
    monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: local_redis)
    return local_redis


def test_takeover_after_holder_kill9(app_lock_on_local_redis):
    """kill -9 持锁进程：存活时拒绝 → 死后接管（短 TTL + 可续期）"""
    redis_client = app_lock_on_local_redis
    lock_key = "eval_task_lock:w10-kill9"
    task_id = "w10-kill9"

    # 自建自清：历史失败运行可能残留本测试专属锁（原持锁 pytest 进程已死，
    # 不清理会让「存活时拒绝」走成接管路径而假红）
    redis_client.delete(lock_key)

    conn_kwargs = redis_client.connection_pool.connection_kwargs
    proc = subprocess.Popen(
        [
            sys.executable, "-c", _CHILD_SRC, lock_key,
            str(conn_kwargs.get("host", "127.0.0.1")),
            str(conn_kwargs.get("port", 6379)),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        line = proc.stdout.readline().decode().strip()
        assert "LOCKED" in line, f"子进程未成功持锁: {line}"

        lock = EvalTaskLock(task_id)
        # 持锁进程存活：拒绝（不接管）
        assert lock.acquire() is False
        assert lock.holder() == f"{socket.gethostname()}:{proc.pid}"
    finally:
        proc.kill()      # SIGKILL：模拟 worker OOM/强杀，锁无法主动释放
        proc.wait()      # 收尸（僵尸进程 os.kill(pid,0) 仍成功，必须 wait）
        proc.stdout.close()

    assert _wait_pid_gone(proc.pid), "子进程收尸后 pid 应从进程表消失"

    # 崩溃残留锁：接管成功（无需等 3 小时 TTL 过期）
    lock = EvalTaskLock(task_id)
    assert lock.acquire() is True
    assert lock.holder() == f"{socket.gethostname()}:{os.getpid()}"

    # 接管锁为短 TTL（≤15 分钟），而非原持有者的 1 小时
    pttl = redis_client.pttl(lock_key)
    assert 0 < pttl <= EVAL_TASK_LOCK_TAKEOVER_TTL * 1000, (
        f"接管锁 TTL 应为短 TTL，实际 pttl={pttl}ms"
    )

    # 任务开跑前续期到全量 TTL（覆盖 2h 任务时限）
    assert lock.renew() is True
    assert redis_client.pttl(lock_key) > EVAL_TASK_LOCK_TAKEOVER_TTL * 1000

    lock.release()
    assert redis_client.get(lock_key) is None
