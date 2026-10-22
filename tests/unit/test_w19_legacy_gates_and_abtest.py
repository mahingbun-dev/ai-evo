"""W19 遗留面门禁 + A/B 会话归属单测

覆盖：
- ensure_super_admin 依赖（deny by default）；
- 整 router 级门禁接线（stats/coverage/claude-code/agent-eval/safety-eval/
  dataset-synthesis/deepseek-harness/legacy excel）；
- 端点级门禁（efficiency 聚合读、docs 写、instruction 批量迁移；工时写入
  PUT /manual-hours 必须保持开放——Tasks.vue 常规流程在用）；
- A/B 会话 created_by 盖章、列表过滤（本人+历史无主行）、读/删归属。
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fastapi import HTTPException

sys.path.insert(0, ".")

from app.services.access_control import ensure_super_admin  # noqa: E402
import app.api.ab_test_api as ab_test_api  # noqa: E402
from app.services.ab_test_service import ABTestService  # noqa: E402

pytestmark = pytest.mark.unit

SUPER = {"user_code": "root", "org_code": "ROOT", "super_admin_flag": 1}
NORMAL = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _has_gate(obj):
    """router 或 route 对象的 dependencies 是否含 ensure_super_admin"""
    deps = getattr(obj, "dependencies", []) or []
    return any(getattr(d, "dependency", None) is ensure_super_admin for d in deps)


class TestEnsureSuperAdmin:
    def test_non_super_403(self):
        with pytest.raises(HTTPException) as ei:
            ensure_super_admin(_req(NORMAL))
        assert ei.value.status_code == 403

    def test_missing_user_403(self):
        with pytest.raises(HTTPException):
            ensure_super_admin(_req(None))

    def test_super_passes(self):
        assert ensure_super_admin(_req(SUPER)) is None


class TestEnsureSuperAdminViaHttp:
    """必须走 TestClient 真实依赖解析：直调函数测不出注入问题。

    事故回归锚点（2026-10-16）：签名写成 request: Any 时，FastAPI 把
    request 解析成必填 query 参数，router 级门禁端点对所有人（含超管）
    一律 422 missing query param——上面的直调用例全部照常通过。
    """

    @staticmethod
    def _client(user):
        from fastapi import APIRouter, Depends, FastAPI, Request
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.middleware("http")
        async def _inject_user(request: Request, call_next):
            if user is not None:
                request.state.user = user
            return await call_next(request)

        router = APIRouter(prefix="/gated", dependencies=[Depends(ensure_super_admin)])

        @router.get("")
        async def gated():
            return {"ok": True}

        app.include_router(router)
        return TestClient(app)

    def test_super_passes_without_query_param(self):
        resp = self._client(SUPER).get("/gated")
        assert resp.status_code == 200, f"超管被拒：{resp.status_code} {resp.text[:200]}"
        assert resp.json() == {"ok": True}

    def test_no_422_for_any_user(self):
        """依赖解析失败的 422 不应对任何角色出现（含未登录）"""
        for user in (NORMAL, None):
            resp = self._client(user).get("/gated")
            assert resp.status_code != 422, f"user={user} 出现依赖注入 422：{resp.text[:200]}"

    def test_non_super_403(self):
        assert self._client(NORMAL).get("/gated").status_code == 403

    def test_missing_user_403(self):
        assert self._client(None).get("/gated").status_code == 403


class TestRouterLevelGates:
    ROUTERS = [
        "app.api.stats_api:router",
        "app.api.coverage_api:router",
        "app.api.claude_code_config_api:router",
        "app.api.agent_evaluation_api:router",
        "app.api.safety_evaluation_api:router",
        "app.api.dataset_synthesis_api:router",
        "app.api.deepseek_harness_config_api:router",
        "app.api.endpoints:router",
    ]

    def test_whole_routers_gated(self):
        import importlib
        for spec in self.ROUTERS:
            mod_name, attr = spec.split(":")
            router = getattr(importlib.import_module(mod_name), attr)
            assert _has_gate(router), f"{mod_name} router 未挂超管门禁"

    def test_efficiency_reads_gated_write_open(self):
        from app.api import efficiency_api
        assert not _has_gate(efficiency_api.router)  # router 级不能整体锁死
        by_path = {r.path: r for r in efficiency_api.router.routes}
        assert _has_gate(by_path["/efficiency/summary"])
        assert _has_gate(by_path["/efficiency/task/{task_id}"])
        assert not _has_gate(by_path["/efficiency/manual-hours"]), "工时写入必须对普通用户开放（Tasks.vue 在用）"

    def test_docs_write_gated_read_open(self):
        from app.api import docs_api
        routes = {r.path: r for r in docs_api.router.routes}
        assert _has_gate(routes["/docs/user-guide"])
        # GET /user-guide 保持开放
        get_route = [r for r in docs_api.router.routes if r.path == "/docs/user-guide" and "GET" in r.methods][0]
        assert not _has_gate(get_route)

    def test_instruction_migrate_gated(self):
        from app.api import instruction_config_api
        migrate = [r for r in instruction_config_api.router.routes
                   if getattr(r, "path", "").endswith("migrate-legacy-all")][0]
        assert _has_gate(migrate)


class TestAbTestSessionOwnership:
    # ---- 纯函数口径 ----
    @staticmethod
    def _session(created_by=None):
        s = MagicMock()
        s.created_by = created_by
        return s

    def test_readable_own_and_legacy_and_super(self):
        assert ab_test_api._session_readable(self._session("u1"), NORMAL)
        assert ab_test_api._session_readable(self._session(None), NORMAL)
        assert ab_test_api._session_readable(self._session("u2"), SUPER)
        assert not ab_test_api._session_readable(self._session("u2"), NORMAL)

    def test_deletable_only_creator_or_super(self):
        assert ab_test_api._session_deletable(self._session("u1"), NORMAL)
        assert ab_test_api._session_deletable(self._session("x"), SUPER)
        assert not ab_test_api._session_deletable(self._session(None), NORMAL), "历史无主行普通用户不可删"
        assert not ab_test_api._session_deletable(self._session("u2"), NORMAL)

    # ---- 服务层：sqlite 内存库验证盖章与列表过滤 ----
    def _svc(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.db.session import Base
        import app.models.ab_test_models  # noqa: F401 确保表注册
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[__import__(
            "app.models.ab_test_models", fromlist=["ABTestSession"]).ABTestSession.__table__])
        return ABTestService(sessionmaker(bind=engine)())

    def test_create_stamps_created_by(self):
        svc = self._svc()
        s = svc.create_session(model_a_id="a", model_b_id="b", created_by="u1")
        assert s.created_by == "u1"

    def test_list_filter_regular_sees_own_and_legacy(self):
        svc = self._svc()
        svc.create_session(model_a_id="a", model_b_id="b", created_by="u1")
        svc.create_session(model_a_id="a", model_b_id="b", created_by="u2")
        svc.create_session(model_a_id="a", model_b_id="b", created_by=None)  # 历史无主
        mine = svc.list_sessions(created_by="u1")
        assert mine["total"] == 2
        all_seen = svc.list_sessions(created_by=None)  # 超管
        assert all_seen["total"] == 3

    # ---- API 端点 ----
    @pytest.mark.asyncio
    async def test_save_stamps_creator(self):
        svc = MagicMock()
        svc.create_session.return_value = self._session("u1")
        with patch.object(ab_test_api, "ABTestService", return_value=svc):
            body = ab_test_api.SaveSessionRequest(model_a_id="a", model_b_id="b")
            await ab_test_api.save_session(_req(NORMAL), body, db=MagicMock())
        assert svc.create_session.call_args.kwargs.get("created_by") == "u1"

    @pytest.mark.asyncio
    async def test_get_session_cross_user_403(self):
        svc = MagicMock()
        svc.get_session.return_value = self._session("u2")
        with patch.object(ab_test_api, "ABTestService", return_value=svc):
            with pytest.raises(HTTPException) as ei:
                await ab_test_api.get_session("sid", _req(NORMAL), db=MagicMock())
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_session_legacy_by_normal_403(self):
        svc = MagicMock()
        svc.get_session.return_value = self._session(None)
        with patch.object(ab_test_api, "ABTestService", return_value=svc):
            with pytest.raises(HTTPException) as ei:
                await ab_test_api.delete_session("sid", _req(NORMAL), db=MagicMock())
        assert ei.value.status_code == 403
        svc.delete_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_session_missing_404(self):
        svc = MagicMock()
        svc.get_session.return_value = None
        with patch.object(ab_test_api, "ABTestService", return_value=svc):
            with pytest.raises(HTTPException) as ei:
                await ab_test_api.delete_session("ghost", _req(SUPER), db=MagicMock())
        assert ei.value.status_code == 404
