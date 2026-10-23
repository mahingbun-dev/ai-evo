"""Redis key 构造（集中管理，避免调用方拼错）"""
from .engine import Engine, coerce_engine

PREFIX = "judge_pool"


def conf_key() -> str:
    return f"{PREFIX}:conf"


def leases_key(engine: Engine | str) -> str:
    return f"{PREFIX}:leases:{coerce_engine(engine).value}"


def lease_data_key(engine: Engine | str) -> str:
    return f"{PREFIX}:lease_data:{coerce_engine(engine).value}"


def wait_key(engine: Engine | str) -> str:
    return f"{PREFIX}:wait:{coerce_engine(engine).value}"


def wait_alive_prefix(engine: Engine | str) -> str:
    """等待者存活凭证的 key 前缀（Lua 侧按 waiter_id 拼接后 EXISTS 判活）"""
    return f"{PREFIX}:wait_alive:{coerce_engine(engine).value}:"


def wait_alive_key(engine: Engine | str, waiter_id: str) -> str:
    """等待者存活凭证：排队期间每轮刷新，进程死亡后不再刷新并随 TTL 消失"""
    return f"{wait_alive_prefix(engine)}{waiter_id}"


def scan_lock_key() -> str:
    return f"{PREFIX}:scan_lock"


def peak_key(engine: Engine | str) -> str:
    return f"{PREFIX}:peak:{coerce_engine(engine).value}"
