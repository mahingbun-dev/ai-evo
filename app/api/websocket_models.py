"""
WebSocket 消息模型定义
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatusMessage(BaseModel):
    """任务状态更新消息"""

    type: str = Field(default="status_update", description="消息类型")
    task_id: str = Field(..., description="任务ID")
    status: Optional[str] = Field(None, description="任务状态: pending/running/stopped/completed/failed")
    progress: Optional[float] = Field(None, description="进度百分比 0-100")
    completed_questions: Optional[int] = Field(None, description="已完成题目数")
    total_questions: Optional[int] = Field(None, description="总题目数")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(), description="时间戳")


class WebSocketConnectMessage(BaseModel):
    """WebSocket 连接成功消息"""

    type: str = Field(default="connected", description="消息类型")
    task_id: str = Field(..., description="任务ID")
    message: str = Field(default="WebSocket connected", description="连接信息")


class WebSocketPongMessage(BaseModel):
    """WebSocket 心跳响应消息"""

    type: str = Field(default="pong", description="消息类型")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(), description="时间戳")
