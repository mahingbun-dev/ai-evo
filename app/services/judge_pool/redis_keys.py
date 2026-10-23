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


def scan_lock_key() -> str:
    return f"{PREFIX}:scan_lock"


def peak_key(engine: Engine | str) -> str:
    return f"{PREFIX}:peak:{coerce_engine(engine).value}"
