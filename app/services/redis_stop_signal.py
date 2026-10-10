"""
Redis 停止信号检查器

用于跨进程的任务停止信号，替代 threading.Event。
另含评测任务级互斥锁（EvalTaskLock）：防 celery 孤儿消息重投导致
同一任务并发执行（缺陷B4，挂载于 celery_tasks.run_evaluation_task）。
"""
import os
import socket
from typing import Optional

import redis
from functools import lru_cache
from app.core.config import get_settings

# TTL: 1 小时
STOP_SIGNAL_TTL = 3600

# 评测任务互斥锁 TTL：需覆盖单任务最长执行时限（celery task_time_limit=7200s），
# worker 崩溃（SIGKILL，锁无法主动释放）时由 TTL 兜底过期
EVAL_TASK_LOCK_TTL = 3 * 3600


@lru_cache
def get_redis_client() -> redis.Redis:
    """获取 Redis 客户端（带缓存）"""
    settings = get_settings()
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


class RedisStopSignalChecker:
    """
    Redis 停止信号检查器

    接口兼容 threading.Event，使现有代码中的 stop_event.is_set() 调用无需修改。
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._key = f"task_stop:{task_id}"

    def is_set(self) -> bool:
        """检查停止信号是否已设置"""
        client = get_redis_client()
        return client.exists(self._key) > 0

    def set(self):
        """设置停止信号"""
        client = get_redis_client()
        client.set(self._key, "1", ex=STOP_SIGNAL_TTL)

    def cleanup(self):
        """清理停止信号"""
        client = get_redis_client()
        client.delete(self._key)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False


class EvalTaskLock:
    """
    评测任务级互斥锁（缺陷B4）

    背景：celery 配置 task_acks_late=True + prefork，worker 进程崩溃（OOM/SIGKILL）
    后未 ACK 的消息会重投，同一评测任务可能在两个 worker 上并发执行——
    任务级 hub 镜像目录出现 rmtree vs hardlink 竞态，脚本误报匹配 0 文件。

    语义：SET NX EX 抢锁；抢不到说明已有 worker 在执行同一任务，调用方应
    直接拒绝本次执行（不排队重入）。锁值写入持锁 worker 标识（hostname:pid），
    便于排查。任务结束调用 release 释放；worker 崩溃残留的锁由 TTL 兜底过期。
    """

    def __init__(self, task_id: str, ttl: int = EVAL_TASK_LOCK_TTL):
        self.task_id = task_id
        self._key = f"eval_task_lock:{task_id}"
        self._ttl = ttl

    def acquire(self) -> bool:
        """抢锁（SET NX EX）：False 表示已有 worker 持有同任务锁"""
        client = get_redis_client()
        holder = f"{socket.gethostname()}:{os.getpid()}"
        return bool(client.set(self._key, holder, nx=True, ex=self._ttl))

    def release(self):
        """释放锁（任务结束调用；worker 崩溃未释放由 TTL 过期兜底）"""
        client = get_redis_client()
        client.delete(self._key)

    def holder(self) -> Optional[str]:
        """查询当前持锁 worker 标识（无锁/异常返回 None，仅排查用）"""
        client = get_redis_client()
        value = client.get(self._key)
        return value if isinstance(value, str) else None
