"""
A/B Test Service 单元测试
测试 ABTestSession 的 CRUD 操作
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from app.models.ab_test_models import ABTestSession
from app.services.ab_test_service import ABTestService


class TestABTestSession:
    """ABTestSession 模型测试"""

    def test_ab_test_session_model_exists(self):
        """测试 ABTestSession 模型存在"""
        assert ABTestSession is not None

    def test_ab_test_session_has_required_fields(self):
        """测试 ABTestSession 有所需字段"""
        session = ABTestSession(
            id="abtest_123",
            name="Test Session",
            model_a_id="model_a",
            model_b_id="model_b",
            model_a_name="Model A",
            model_b_name="Model B",
        )
        assert session.id == "abtest_123"
        assert session.name == "Test Session"
        assert session.model_a_id == "model_a"
        assert session.model_b_id == "model_b"


class TestABTestService:
    """ABTestService 测试"""

    def test_create_session(self):
        """测试创建 A/B Test 会话"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)

        session = service.create_session(
            name="Test Session",
            model_a_id="model_a_123",
            model_b_id="model_b_456",
            model_a_name="Model A",
            model_b_name="Model B",
        )

        # 验证 ID 以 abtest_ 开头
        assert session.id.startswith("abtest_")
        assert session.name == "Test Session"
        assert session.model_a_id == "model_a_123"
        assert session.model_b_id == "model_b_456"

    def test_get_session(self):
        """测试获取 A/B Test 会话"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)

        # 创建一个模拟的 session
        mock_session = ABTestSession(
            id="abtest_test123",
            name="Test Session",
            model_a_id="model_a",
            model_b_id="model_b",
            model_a_name="Model A",
            model_b_name="Model B",
        )
        mock_db.query.return_value.filter.return_value.first.return_value = mock_session

        result = service.get_session("abtest_test123")

        assert result is not None
        assert result.id == "abtest_test123"
        assert result.model_a_id == "model_a"
        assert result.model_b_id == "model_b"

    def test_get_session_not_found(self):
        """测试获取不存在的会话返回 None"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = service.get_session("nonexistent")

        assert result is None

    def test_list_sessions(self):
        """测试分页查询会话"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)

        mock_sessions = [
            ABTestSession(id="abtest_1", name="Session 1", model_a_id="a", model_b_id="b", model_a_name="A", model_b_name="B"),
            ABTestSession(id="abtest_2", name="Session 2", model_a_id="c", model_b_id="d", model_a_name="C", model_b_name="D"),
        ]
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.offset.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = mock_sessions
        mock_query.count.return_value = 2
        mock_db.query.return_value = mock_query

        result = service.list_sessions(page=1, page_size=10)

        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["page"] == 1
        assert result["page_size"] == 10

    def test_delete_session(self):
        """测试删除会话"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)

        # 创建一个模拟的 session
        mock_session = ABTestSession(
            id="abtest_to_delete",
            name="To Delete",
            model_a_id="a",
            model_b_id="b",
            model_a_name="A",
            model_b_name="B",
        )
        mock_db.query.return_value.filter.return_value.first.return_value = mock_session

        result = service.delete_session("abtest_to_delete")

        assert result is True
        mock_db.delete.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_delete_session_not_found(self):
        """测试删除不存在的会话返回 False"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = service.delete_session("nonexistent")

        assert result is False

    def test_list_sessions_with_keyword(self):
        """测试关键字搜索"""
        mock_db = MagicMock()
        service = ABTestService(mock_db)

        mock_sessions = [
            ABTestSession(id="abtest_search", name="Search Test", model_a_id="a", model_b_id="b", model_a_name="A", model_b_name="B"),
        ]
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.ilike.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.offset.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = mock_sessions
        mock_query.count.return_value = 1
        mock_db.query.return_value = mock_query

        result = service.list_sessions(keyword="Search")

        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0].name == "Search Test"


class TestABTestRoutes:
    """A/B Test API 路由测试"""

    def test_ab_test_router_registered(self):
        """测试 ab_test_router 已注册"""
        from app.main import app
        routes = [r.path for r in app.routes]
        ab_test_routes = [r for r in routes if 'ab-test' in r]
        assert len(ab_test_routes) > 0, f"应有 ab-test 路由，实际路由: {routes}"

    def test_ab_test_websocket_endpoint(self):
        """测试 WebSocket 端点路径正确"""
        from app.api.ab_test_api import router
        ws_routes = [r.path for r in router.routes if hasattr(r, 'path') and 'ws' in r.path]
        assert len(ws_routes) > 0, f"应有 WebSocket 路由，实际路由: {[r.path for r in router.routes if hasattr(r, 'path')]}"
