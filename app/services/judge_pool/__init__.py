"""LLM 裁判全局并发池"""
from .engine import Engine, coerce_engine
from .errors import JudgePoolAborted, JudgePoolError, JudgePoolTimeout

__all__ = [
    "Engine", "coerce_engine",
    "JudgePoolError", "JudgePoolTimeout", "JudgePoolAborted",
]
