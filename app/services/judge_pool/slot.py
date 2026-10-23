"""并发池使用入口：所有裁判调用必须包一层 judge_pool_slot"""
import os
from contextlib import contextmanager

from .client import get_pool_client
from .engine import coerce_engine
from .scanner import get_scanner

_FALSE_VALUES = ("0", "false", "no", "off")


def pool_enabled() -> bool:
    return os.getenv("JUDGE_POOL_ENABLED", "true").strip().lower() not in _FALSE_VALUES


@contextmanager
def judge_pool_slot(engine, meta=None):
    if not pool_enabled():
        yield None
        return
    # 首个使用者启动本进程的扫描线程（每进程至多一个，Redis 锁保证全局单例执行）
    get_scanner()
    lease = get_pool_client().acquire(coerce_engine(engine), meta=meta)
    try:
        yield lease
    finally:
        get_pool_client().release(lease)
