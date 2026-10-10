"""
WebSocket Manager - 管理 WebSocket 客户端连接

单例模式，维护 task_id -> set[WebSocket] 的映射，
支持多客户端订阅同一任务，以及广播消息到所有订阅者。
"""

import asyncio
import logging
from typing import Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        # task_id -> set of WebSocket connections
        self._connections: dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        """接受 WebSocket 连接并添加到指定任务的订阅者集合"""
        await websocket.accept()
        async with self._lock:
            if task_id not in self._connections:
                self._connections[task_id] = set()
            self._connections[task_id].add(websocket)
        logger.info(f"WebSocket connected: task_id={task_id}, total={len(self._connections.get(task_id, set()))}")

    async def disconnect(self, task_id: str, websocket: WebSocket) -> None:
        """从指定任务的订阅者集合中移除 WebSocket 连接"""
        async with self._lock:
            if task_id in self._connections:
                self._connections[task_id].discard(websocket)
                if not self._connections[task_id]:
                    del self._connections[task_id]
        logger.info(f"WebSocket disconnected: task_id={task_id}")

    async def broadcast(self, task_id: str, message: dict) -> None:
        """广播消息到指定任务的所有订阅者"""
        async with self._lock:
            connections = self._connections.get(task_id, set()).copy()
        logger.info(f"Broadcast called: task_id={task_id}, connections={len(connections)}, message={message}")

        disconnected = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to WebSocket: {e}")
                disconnected.append(websocket)

        # 清理断开的连接
        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    if task_id in self._connections:
                        self._connections[task_id].discard(ws)

    def get_subscribed_tasks(self) -> set[str]:
        """返回当前所有订阅中的 task_id 集合"""
        return set(self._connections.keys())

    @property
    def connection_count(self) -> int:
        """返回当前总连接数"""
        return sum(len(conns) for conns in self._connections.values())


# 全局单例
_websocket_manager: WebSocketManager | None = None


def get_websocket_manager() -> WebSocketManager:
    """获取 WebSocket Manager 单例"""
    global _websocket_manager
    if _websocket_manager is None:
        _websocket_manager = WebSocketManager()
    return _websocket_manager


# 方便导入的实例
ws_manager = get_websocket_manager()
