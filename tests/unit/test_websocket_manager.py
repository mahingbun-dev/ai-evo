"""
WebSocket Manager 单元测试
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import WebSocket

from app.api.websocket_manager import WebSocketManager


class TestWebSocketManager:
    """WebSocket Manager 测试类"""

    @pytest.fixture
    def ws_manager(self):
        """创建独立的 WebSocketManager 实例用于测试"""
        return WebSocketManager()

    @pytest.fixture
    def mock_websocket(self):
        """创建模拟的 WebSocket"""
        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, ws_manager, mock_websocket):
        """测试建立连接后断开，验证 task_id 映射正确"""
        task_id = "test_task_1"

        # 建立连接
        await ws_manager.connect(task_id, mock_websocket)
        assert task_id in ws_manager.get_subscribed_tasks()
        assert len(ws_manager._connections.get(task_id, set())) == 1

        # 断开连接
        await ws_manager.disconnect(task_id, mock_websocket)
        assert task_id not in ws_manager.get_subscribed_tasks()

    @pytest.mark.asyncio
    async def test_multi_client_broadcast(self, ws_manager, mock_websocket):
        """测试同一 task_id 建立多个 WebSocket 连接，broadcast 时所有客户端收到消息"""
        task_id = "test_task_2"

        # 创建多个模拟 WebSocket
        ws1 = AsyncMock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()

        ws2 = AsyncMock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()

        # 建立两个连接
        await ws_manager.connect(task_id, ws1)
        await ws_manager.connect(task_id, ws2)

        assert len(ws_manager._connections.get(task_id, set())) == 2

        # 广播消息
        message = {"type": "status_update", "task_id": task_id}
        await ws_manager.broadcast(task_id, message)

        # 验证两个客户端都收到消息
        assert ws1.send_json.called
        assert ws2.send_json.called
        ws1.send_json.assert_called_once_with(message)
        ws2.send_json.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_get_subscribed_tasks(self, ws_manager, mock_websocket):
        """测试连接多个 task_id，验证返回正确的任务集合"""
        # 建立多个任务的连接
        await ws_manager.connect("task_1", mock_websocket)

        ws2 = AsyncMock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()

        await ws_manager.connect("task_2", ws2)

        ws3 = AsyncMock(spec=WebSocket)
        ws3.accept = AsyncMock()
        ws3.send_json = AsyncMock()

        await ws_manager.connect("task_3", ws3)

        subscribed = ws_manager.get_subscribed_tasks()
        assert subscribed == {"task_1", "task_2", "task_3"}

    @pytest.mark.asyncio
    async def test_disconnect_non_existent_task(self, ws_manager, mock_websocket):
        """测试断开不存在的任务连接，不抛出异常"""
        await ws_manager.disconnect("non_existent_task", mock_websocket)
        # 不应抛出异常
        assert ws_manager.connection_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_to_no_subscribers(self, ws_manager):
        """测试向没有订阅者的任务广播消息，不抛出异常"""
        await ws_manager.broadcast("no_subscribers_task", {"type": "test"})
        # 不应抛出异常

    @pytest.mark.asyncio
    async def test_connection_count(self, ws_manager, mock_websocket):
        """测试连接计数正确"""
        ws1 = AsyncMock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()

        ws2 = AsyncMock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()

        await ws_manager.connect("task_1", mock_websocket)
        await ws_manager.connect("task_1", ws1)
        await ws_manager.connect("task_2", ws2)

        assert ws_manager.connection_count == 3

    @pytest.mark.asyncio
    async def test_broadcast_removes_disconnected(self, ws_manager):
        """测试广播时移除断开的连接"""
        task_id = "test_task_remove"

        ws1 = AsyncMock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()

        ws2 = AsyncMock(spec=WebSocket)
        ws2.accept = AsyncMock()
        # ws2.send_json 抛出异常，模拟断开连接
        ws2.send_json = AsyncMock(side_effect=Exception("Connection closed"))

        await ws_manager.connect(task_id, ws1)
        await ws_manager.connect(task_id, ws2)

        assert ws_manager.connection_count == 2

        # 广播应该移除发送失败的连接
        await ws_manager.broadcast(task_id, {"type": "test"})

        # ws1 发送成功，ws2 因异常被移除
        assert ws_manager.connection_count == 1


class TestRedisPubSubHandler:
    """Redis Pub/Sub Handler 测试类"""

    @pytest.fixture
    def mock_redis(self):
        """创建模拟的 Redis 连接"""
        with patch("redis.from_url") as mock:
            redis_mock = MagicMock()
            pubsub_mock = MagicMock()
            redis_mock.pubsub.return_value = pubsub_mock
            mock.return_value = redis_mock
            yield redis_mock, pubsub_mock

    def test_subscribe_channel(self, mock_redis):
        """测试订阅频道"""
        from app.api.pubsub_handler import PubSubListener

        redis_conn, pubsub_mock = mock_redis
        listener = PubSubListener()

        listener.subscribe("test_task")

        pubsub_mock.subscribe.assert_called_once_with("task_status:test_task")
        assert "test_task" in listener._subscribed_channels

    def test_unsubscribe_channel(self, mock_redis):
        """测试取消订阅频道"""
        from app.api.pubsub_handler import PubSubListener

        redis_conn, pubsub_mock = mock_redis
        listener = PubSubListener()

        listener.subscribe("test_task")
        listener.unsubscribe("test_task")

        pubsub_mock.unsubscribe.assert_called_once_with("task_status:test_task")
        assert "test_task" not in listener._subscribed_channels

    def test_subscribe_duplicate(self, mock_redis):
        """测试重复订阅不重复注册"""
        from app.api.pubsub_handler import PubSubListener

        redis_conn, pubsub_mock = mock_redis
        listener = PubSubListener()

        listener.subscribe("test_task")
        listener.subscribe("test_task")

        # 只应该订阅一次
        assert pubsub_mock.subscribe.call_count == 1
