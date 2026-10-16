"""
WebSocket API 端点单元测试
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import WebSocket

from app.api.task_status_api import router, websocket_endpoint

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


@pytest.fixture(autouse=True)
def _hermetic_jwt_secret(monkeypatch):
    """固定显式 JWT 密钥，脱离所在机器的 .env（ARCH-0-2）。

    JWT 密钥不再默认从 DATABASE_URL 派生（缺密钥即抛错），本文件的
    mock_websocket 调真实 create_token 签发——CI 干净环境（无 .env）会必红，
    统一 patch 为显式密钥保持密闭。
    """
    from app.core.config import Settings
    from app.services import jwt_service
    monkeypatch.setattr(
        jwt_service, "get_settings",
        lambda: Settings(
            _env_file=None,
            DATABASE_URL="postgresql://unit-test",
            AUTH_JWT_SECRET="unit-test-jwt-secret",
        ),
    )


class TestWebSocketAPI:
    """WebSocket API 端点测试类"""

    @pytest.fixture
    def mock_websocket(self):
        """创建模拟的 WebSocket（携带有效登录 token，通过端点内 JWT 校验）"""
        from types import SimpleNamespace
        from app.services.jwt_service import create_token

        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.receive_text = AsyncMock()
        ws.close = AsyncMock()
        ws.query_params = SimpleNamespace(
            **{"get": lambda key, default=None: create_token("u_test", "tester") if key == "token" else default}
        )
        return ws

    @pytest.fixture
    def auth_passthrough(self):
        """
        放行鉴权与归属校验（W5 起端点 accept 前统一鉴权）。

        本测试类的意图是验证连接/心跳/断开清理逻辑，
        鉴权与归属校验本身由独立测试负责（见整改计划 W5 测试设计）。
        """
        fake_user = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}
        with patch("app.api.task_status_api.require_ws_user", new_callable=AsyncMock) as m_auth, \
             patch("app.api.task_status_api._check_task_ownership", new_callable=AsyncMock) as m_owner:
            m_auth.return_value = fake_user
            m_owner.return_value = True
            yield m_auth, m_owner

    @pytest.mark.asyncio
    async def test_websocket_connect(self, mock_websocket, auth_passthrough):
        """测试建立 WebSocket 连接，验证连接成功"""
        task_id = "test_task_1"

        with patch("app.api.task_status_api.ws_manager") as mock_ws_manager:
            mock_ws_manager.connect = AsyncMock()
            mock_ws_manager.disconnect = AsyncMock()

            # 模拟接收 - 立即抛出 WebSocketDisconnect
            mock_websocket.receive_text = AsyncMock(side_effect=Exception("Test disconnect"))

            try:
                await websocket_endpoint(mock_websocket, task_id)
            except Exception:
                pass

            # 验证 connect 被调用
            mock_ws_manager.connect.assert_called_once_with(task_id, mock_websocket)

    @pytest.mark.asyncio
    async def test_websocket_heartbeat(self, mock_websocket, auth_passthrough):
        """测试发送 ping 后收到 pong 回复"""
        task_id = "test_task_1"
        messages = ["ping", Exception("Disconnect")]

        with patch("app.api.task_status_api.ws_manager") as mock_ws_manager:
            mock_ws_manager.connect = AsyncMock()
            mock_ws_manager.disconnect = AsyncMock()

            mock_websocket.receive_text = AsyncMock(side_effect=lambda: (_ for _ in ()).throw(Exception("Disconnect")) if len(messages) == 0 else messages.pop(0))
            # First call returns "ping", second call throws disconnect

            async def receive_side_effect():
                if len(messages) > 0:
                    msg = messages.pop(0)
                    if isinstance(msg, Exception):
                        raise msg
                    return msg
                raise Exception("Test end")

            mock_websocket.receive_text = AsyncMock(side_effect=receive_side_effect)

            try:
                await websocket_endpoint(mock_websocket, task_id)
            except Exception:
                pass

            # 验证发送了 pong 消息
            assert mock_websocket.send_json.called

    @pytest.mark.asyncio
    async def test_websocket_disconnect(self, mock_websocket, auth_passthrough):
        """测试断开连接，验证清理逻辑"""
        task_id = "test_task_disconnect"

        with patch("app.api.task_status_api.ws_manager") as mock_ws_manager:
            mock_ws_manager.connect = AsyncMock()
            mock_ws_manager.disconnect = AsyncMock()

            # 立即断开
            mock_websocket.receive_text = AsyncMock(side_effect=Exception("Client disconnected"))

            try:
                await websocket_endpoint(mock_websocket, task_id)
            except Exception:
                pass

            # 验证 disconnect 被调用
            mock_ws_manager.disconnect.assert_called()

    @pytest.mark.asyncio
    async def test_websocket_invalid_task_id(self, mock_websocket, auth_passthrough):
        """测试使用无效 task_id 连接，验证错误处理"""
        task_id = ""

        with patch("app.api.task_status_api.ws_manager") as mock_ws_manager:
            mock_ws_manager.connect = AsyncMock()
            mock_ws_manager.disconnect = AsyncMock()

            mock_websocket.receive_text = AsyncMock(side_effect=Exception("Test end"))

            try:
                await websocket_endpoint(mock_websocket, task_id)
            except Exception:
                pass

            # 即使是空 task_id，也应该调用 connect
            mock_ws_manager.connect.assert_called_once()

    def test_router_exists(self):
        """测试 router 存在"""
        assert router is not None
