"""裁判引擎分类：并发池按引擎独立限额"""
from enum import Enum


class Engine(str, Enum):
    GRAPH = "graph"
    CLAUDE_AGENT = "claude_agent"
    HARNESS = "harness"
    PLUGIN = "plugin"


def coerce_engine(value) -> Engine:
    if isinstance(value, Engine):
        return value
    try:
        return Engine(str(value))
    except ValueError as e:
        raise ValueError(f"未知裁判引擎: {value!r}") from e
