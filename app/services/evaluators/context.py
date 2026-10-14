"""评测执行上下文（W17）

执行分发收敛到注册表后，各类型执行钩子需要的运行时参数统一由
``EvaluationContext`` 携带；字段与 ``evaluation_tasks_async._execute_evaluation``
现分发给各专用路径的实参一一对应。
"""
from dataclasses import dataclass
from typing import Any, Optional, Protocol


class StopEventProtocol(Protocol):
    """停止事件协议 —— 兼容 threading.Event 与 RedisStopSignalChecker"""

    def is_set(self) -> bool: ...


@dataclass
class EvaluationContext:
    """单次评测执行的运行时上下文

    Attributes:
        db: 数据库会话（行级落库/配置回退查询用；直调链路可能为 None）
        task_id: 任务 ID（日志、进度、临时目录隔离）
        stop_event: 停止信号（threading.Event / Redis 停止检查器）
        execution_number: 行级落库执行序号（W3）；None（直调/测试）时各路径跳过行级落库
        progress_offset: 断点续跑进度偏移（已完成行数；latency 恒 0）
        langfuse_host: LangFuse 地址（未启用为空串）
    """

    db: Optional[Any]
    task_id: str
    stop_event: Optional[StopEventProtocol] = None
    execution_number: Optional[int] = None
    progress_offset: int = 0
    langfuse_host: str = ""
