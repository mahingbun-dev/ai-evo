"""W19 模型配置权限收口单测

覆盖：
- ensure_model_access 原语（404/403/超管放行/本团队放行）；
- model_config_api 端点门禁接线（详情/更新/删除/测连调用 ensure_model_access，
  创建盖创建人团队，variables 读保持开放——只回变量名不含凭据）；
- WS 测连归属校验（4404 不存在 / 4403 跨团队）。
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

sys.path.insert(0, ".")

from app.services.access_control import (  # noqa: E402
    AccessDeniedError,
    ensure_model_access,
)
import app.api.model_config_api as model_config_api  # noqa: E402

pytestmark = pytest.mark.unit


def _db_find(record):
    """构造 db.query(...).filter(...).first() 链 mock"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = lambda: record
    return db


SUPER = {"user_code": "root", "org_code": "ROOT", "super_admin_flag": 1}
OWNER = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}
OTHER = {"user_code": "u2", "org_code": "T2", "super_admin_flag": 0}


def _model(team_code="T1"):
    m = MagicMock()
    m.team_code = team_code
    return m


class TestEnsureModelAccess:
    def test_same_team_ok(self):
        rec = _model("T1")
        assert ensure_model_access(_db_find(rec), OWNER, "mid") is rec

    def test_super_admin_cross_team_ok(self):
        rec = _model("T2")
        assert ensure_model_access(_db_find(rec), SUPER, "mid") is rec

    def test_cross_team_403(self):
        rec = _model("T2")
        with pytest.raises(HTTPException) as ei:
            ensure_model_access(_db_find(rec), OWNER, "mid")
        assert ei.value.status_code == 403

    def test_missing_404_no_leak(self):
        with pytest.raises(HTTPException) as ei:
            ensure_model_access(_db_find(None), OWNER, "mid")
        assert ei.value.status_code == 404
        assert ei.value.detail == "模型不存在"

    def test_null_team_defaults_to_DEFAULT(self):
        rec = _model(None)
        u = {"user_code": "u1", "org_code": "", "super_admin_flag": 0}
        # 双侧空按 DEFAULT 兜底 → 同团队放行
        assert ensure_model_access(_db_find(rec), u, "mid") is rec


class TestModelApiGateWiring:
    """端点接线：门禁被调用且参数正确（语义由原语测试覆盖）"""

    def _req(self, user):
        return SimpleNamespace(state=SimpleNamespace(user=user))

    def test_get_model_calls_ensure(self):
        rec = _model("T1")
        with patch.object(model_config_api, "ensure_model_access", return_value=rec) as pm:
            out = model_config_api.get_model("mid", self._req(OWNER), db=MagicMock())
        pm.assert_called_once()
        assert out is rec

    def test_update_model_gate_before_mutation(self):
        svc = MagicMock()
        svc.update_model.return_value = _model("T1")
        with patch.object(model_config_api, "ensure_model_access") as pm, \
             patch.object(model_config_api, "ModelConfigService", return_value=svc):
            body = model_config_api.UpdateModelRequest(description="x")
            model_config_api.update_model("mid", self._req(OWNER), body, db=MagicMock())
        pm.assert_called_once()
        svc.update_model.assert_called_once()

    def test_delete_model_gate(self):
        svc = MagicMock()
        svc.delete_model.return_value = True
        with patch.object(model_config_api, "ensure_model_access") as pm, \
             patch.object(model_config_api, "ModelConfigService", return_value=svc):
            model_config_api.delete_model("mid", self._req(OWNER), db=MagicMock())
        pm.assert_called_once()

    @pytest.mark.asyncio
    async def test_test_connection_gate(self):
        svc = MagicMock()
        svc.test_connection = AsyncMock(return_value={"success": True})
        with patch.object(model_config_api, "ensure_model_access") as pm, \
             patch.object(model_config_api, "ModelConfigService", return_value=svc):
            await model_config_api.test_model_connection(
                "mid", self._req(OWNER), body=None, db=MagicMock())
        pm.assert_called_once()

    def test_create_model_stamps_team_code(self):
        svc = MagicMock()
        svc.create_model.return_value = _model("T1")
        with patch.object(model_config_api, "ModelConfigService", return_value=svc):
            body = model_config_api.CreateModelRequest(name="m", provider="openai", model_type="gpt")
            model_config_api.create_model(self._req(OWNER), body, db=MagicMock())
        assert svc.create_model.call_args.kwargs.get("team_code") == "T1"

    def test_judge_create_stamps_team_code(self):
        svc = MagicMock()
        svc.create_model.return_value = _model("T1")
        with patch.object(model_config_api, "ModelConfigService", return_value=svc):
            body = model_config_api.CreateModelRequest(name="j", provider="openai", model_type="gpt")
            model_config_api.create_judge_model(self._req(OWNER), body, db=MagicMock())
        kwargs = svc.create_model.call_args.kwargs
        assert kwargs.get("team_code") == "T1" and kwargs.get("model_role") == "judge"

    def test_judge_detail_gated(self):
        rec = _model("T1")
        with patch.object(model_config_api, "ensure_model_access", return_value=rec) as pm:
            model_config_api.get_judge_model("jid", self._req(OWNER), db=MagicMock())
        pm.assert_called_once()

    def test_variables_endpoint_not_gated(self):
        """变量名列表开放（无 ensure_model_access 依赖；响应只含变量名）"""
        svc = MagicMock()
        svc.get_model.return_value = MagicMock()
        with patch.object(model_config_api, "ensure_model_access") as pm, \
             patch.object(model_config_api, "ModelConfigService", return_value=svc), \
             patch("app.services.request_chain.RequestChainExecutor") as rce:
            rce.extract_variables.return_value = []
            model_config_api.get_model_variables("mid", db=MagicMock())
        pm.assert_not_called()


class TestModelWsOwnership:
    @staticmethod
    def _ws():
        ws = MagicMock()
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_missing_model_close_4404(self):
        ws = self._ws()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with patch.object(model_config_api, "require_ws_user",
                          new=AsyncMock(return_value=OWNER)), \
             patch("app.db.session.SessionLocal", return_value=MagicMock(__enter__=lambda s: db, __exit__=lambda s, *a: False)):
            await model_config_api.websocket_test_model(ws, "mid")
        ws.close.assert_awaited_once_with(code=4404)

    @pytest.mark.asyncio
    async def test_cross_team_close_4403(self):
        ws = self._ws()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _model("T2")
        with patch.object(model_config_api, "require_ws_user",
                          new=AsyncMock(return_value=OWNER)), \
             patch("app.db.session.SessionLocal", return_value=MagicMock(__enter__=lambda s: db, __exit__=lambda s, *a: False)):
            await model_config_api.websocket_test_model(ws, "mid")
        ws.close.assert_awaited_once_with(code=4403)

    @pytest.mark.asyncio
    async def test_same_team_accepts(self):
        ws = self._ws()
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock(return_value="")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _model("T1")
        with patch.object(model_config_api, "require_ws_user",
                          new=AsyncMock(return_value=OWNER)), \
             patch("app.db.session.SessionLocal", return_value=MagicMock(__enter__=lambda s: db, __exit__=lambda s, *a: False)), \
             patch.object(model_config_api, "ModelConfigService") as svc:
            svc.return_value.test_connection = AsyncMock(return_value={"success": True})
            # 端点内 after accept 走 try 并在 receive_text 后进入执行链，这里只验证 accept 已发生
            try:
                await model_config_api.websocket_test_model(ws, "mid")
            except Exception:
                pass
        ws.accept.assert_awaited_once()
