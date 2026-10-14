"""W19 用户目录门禁 + 禁用账号拦截单测

覆盖：
- /users 列表与详情仅超管（403 deny by default，/users/me 不受限）；
- auth_middleware 对 is_active=False 账号返回 403；
- ws_auth.require_ws_user 对禁用账号 close(4403)。
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

sys.path.insert(0, ".")

import app.api.user_api as user_api  # noqa: E402
from app.middleware.auth_middleware import AuthMiddleware  # noqa: E402
from app.services.ws_auth import require_ws_user  # noqa: E402

pytestmark = pytest.mark.unit

SUPER = {"user_code": "root", "org_code": "ROOT", "super_admin_flag": 1}
NORMAL = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


class TestUserDirectoryGate:
    @pytest.mark.asyncio
    async def test_list_users_non_super_403(self):
        db = MagicMock()
        with pytest.raises(HTTPException) as ei:
            await user_api.list_users(_req(NORMAL), page=1, page_size=20, keyword=None, db=db)
        assert ei.value.status_code == 403
        db.query.assert_not_called()  # 门禁先于任何查询

    @pytest.mark.asyncio
    async def test_list_users_super_ok(self):
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value = q
        q.count.return_value = 0
        q.offset.return_value.limit.return_value.all.return_value = []
        out = await user_api.list_users(_req(SUPER), page=1, page_size=20, keyword=None, db=db)
        assert out.total == 0

    @pytest.mark.asyncio
    async def test_get_user_non_super_403(self):
        db = MagicMock()
        with pytest.raises(HTTPException) as ei:
            await user_api.get_user(_req(NORMAL), "someone", db=db)
        assert ei.value.status_code == 403
        db.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_user_super_missing_404(self):
        db = MagicMock()
        db.get.return_value = None
        with pytest.raises(HTTPException) as ei:
            await user_api.get_user(_req(SUPER), "ghost", db=db)
        assert ei.value.status_code == 404


def _ctx(session):
    return MagicMock(__enter__=lambda s: session, __exit__=lambda s, *a: False)


class TestDisabledUserMiddleware:
    @staticmethod
    def _http_request():
        return SimpleNamespace(
            url=SimpleNamespace(path="/api/v1/models"),
            headers={"authorization": "Bearer tok"},
            scope={"type": "http"},
            state=SimpleNamespace(),
        )

    @staticmethod
    def _orm_user(is_active=True, user_code="u1"):
        u = MagicMock()
        u.is_active = is_active
        u.user_code = user_code
        for f in ("user_name", "email", "employee_id", "org_code", "org_name",
                  "position_name", "user_type", "super_admin_flag", "application_admin"):
            setattr(u, f, None)
        u.last_login_at = None
        u.created_at = None
        u.updated_at = None
        return u

    @pytest.mark.asyncio
    async def test_disabled_user_403_and_no_downstream(self):
        called = {"next": False}

        async def call_next(req):
            called["next"] = True

        mw = AuthMiddleware(app=None)
        session = MagicMock()
        session.get.return_value = self._orm_user(is_active=False)
        with patch("app.middleware.auth_middleware.decode_token",
                   return_value={"sub": "u1", "username": "u1"}), \
             patch("app.middleware.auth_middleware.SessionLocal", return_value=_ctx(session)):
            resp = await mw.dispatch(self._http_request(), call_next)
        assert resp.status_code == 403
        assert "禁用" in resp.body.decode()
        assert not called["next"]

    @pytest.mark.asyncio
    async def test_active_user_passes_through(self):
        sentinel = SimpleNamespace(status_code=200)

        async def call_next(req):
            return sentinel

        mw = AuthMiddleware(app=None)
        session = MagicMock()
        session.get.return_value = self._orm_user(is_active=True)
        with patch("app.middleware.auth_middleware.decode_token",
                   return_value={"sub": "u1", "username": "u1"}), \
             patch("app.middleware.auth_middleware.SessionLocal", return_value=_ctx(session)):
            resp = await mw.dispatch(self._http_request(), call_next)
        assert resp is sentinel


class TestDisabledUserWs:
    @staticmethod
    def _ws():
        ws = MagicMock()
        ws.query_params = {"token": "tok"}
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_disabled_user_close_4403(self):
        ws = self._ws()
        u = MagicMock()
        u.is_active = False
        u.user_code = "u1"
        session = MagicMock()
        session.get.return_value = u
        with patch("app.services.ws_auth.decode_token",
                   return_value={"sub": "u1", "username": "u1"}), \
             patch("app.services.ws_auth.SessionLocal", return_value=_ctx(session)):
            out = await require_ws_user(ws)
        assert out is None
        ws.close.assert_awaited_once_with(code=4403)

    @pytest.mark.asyncio
    async def test_active_user_returns_dict(self):
        ws = self._ws()
        u = MagicMock()
        u.is_active = True
        u.user_code = "u1"
        u.username = "u1"
        u.user_name = None
        u.email = None
        u.employee_id = None
        u.org_code = "T1"
        u.org_name = None
        u.position_name = None
        u.user_type = None
        u.super_admin_flag = 0
        u.application_admin = None
        u.last_login_at = None
        u.created_at = None
        u.updated_at = None
        session = MagicMock()
        session.get.return_value = u
        with patch("app.services.ws_auth.decode_token",
                   return_value={"sub": "u1", "username": "u1"}), \
             patch("app.services.ws_auth.SessionLocal", return_value=_ctx(session)):
            out = await require_ws_user(ws)
        assert out["user_code"] == "u1"
        ws.close.assert_not_awaited()
