"""RBAC 三角色单元测试（M4 QE4-3）：权限矩阵穷举 + API 级角色闸

覆盖（方案 §4.3 / §4.5「权限矩阵穷举单测」）：
- 矩阵穷举：三角色 × 全资源 × read/write/manage 三动作，逐一断言与
  语义不变量一致（viewer 处处无 write；owner/engineer 写权限等价；
  manage 仅 owner 的 team；脚本库/裁判配置不进矩阵=全员 deny）；
- user_role 兜底：NULL/未知值按 engineer；
- ensure_permission：超管放行、viewer 403（文案含角色与资源）、owner 放行；
- API 级：viewer JWT 打写端点 403、engineer 过角色闸（非 403）、
  同为 viewer 的读端点不受影响；
- 角色管理端点：本团队 owner 可改、engineer 403、跨团队 owner 403、
  末位 owner 降级 409、非法角色 400、审计留痕、成员列表带 role；
- 登录/注册响应携带 role；users.role 未回填的历史行（NULL）按 engineer。
"""
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping, LdapConfig
from app.models.team_models import Team
from app.models.user_models import User
from app.services import jwt_service
from app.services.access_control import (
    PERMISSIONS, ROLE_OWNER, ROLE_ENGINEER, ROLE_VIEWER, TEAM_ROLES,
    ensure_permission, role_can, user_role,
)
from app.services import sso_service
from app.api import sso_api, user_api, team_api

pytestmark = pytest.mark.unit  # W8 分类门禁：sqlite 内存库 + 全替身


# ==================== 1. 权限矩阵穷举 ====================

MATRIX_RESOURCES = ["tasks", "datasets", "models", "templates",
                    "media", "webhooks", "api_keys", "team"]


class TestPermissionMatrix:
    def test_matrix_exhaustive_semantics(self):
        """穷举三角色 × 全资源 × 三动作，断言语义不变量（方案口径）"""
        for role in TEAM_ROLES:
            assert role in PERMISSIONS, f"{role} 应在矩阵中"
            for resource in MATRIX_RESOURCES:
                for action in ("read", "write", "manage"):
                    allowed = role_can(role, resource, action)
                    if role == ROLE_VIEWER:
                        # viewer 只读：任何资源都无 write/manage
                        assert not allowed or action == "read", \
                            f"viewer 不得拥有 {resource}:{action}"
                    if action == "manage":
                        # manage（角色管理）仅 owner 的 team
                        assert allowed == (role == ROLE_OWNER and resource == "team"), \
                            f"manage 权限越界：{role}:{resource}"
                    if role in (ROLE_OWNER, ROLE_ENGINEER):
                        if resource == "team":
                            # owner/engineer 的 team 均可读，owner 另有 manage
                            assert role_can(role, resource, "read")
                        else:
                            assert role_can(role, resource, "read") and \
                                role_can(role, resource, "write"), \
                                f"{role} 对 {resource} 应可读写"

    def test_owner_engineer_write_equivalence(self):
        """写操作=owner+engineer（两角色写权限逐资源等价；team 是 manage
        语义资源不参与 write 等价比较）"""
        write_resources = [r for r in MATRIX_RESOURCES if r != "team"]
        for resource in write_resources:
            assert role_can(ROLE_OWNER, resource, "write") is True
            assert role_can(ROLE_ENGINEER, resource, "write") is True

    def test_scripts_and_judge_not_in_matrix(self):
        """脚本库写、裁判配置维持超管：不进矩阵（缺资源=全员 deny）"""
        for role in TEAM_ROLES:
            assert not role_can(role, "scripts", "write")
            assert not role_can(role, "judge_config", "write")

    def test_unknown_role_or_resource_denied(self):
        assert not role_can("admin", "tasks", "write")
        assert not role_can(ROLE_OWNER, "nonexistent", "read")

    def test_all_roles_can_read_all_resources(self):
        """只读=viewer：所有角色对所有矩阵资源可读"""
        for role in TEAM_ROLES:
            for resource in MATRIX_RESOURCES:
                assert role_can(role, resource, "read"), f"{role} 应可读 {resource}"


class TestUserRoleFallback:
    def test_null_defaults_engineer(self):
        assert user_role({"role": None}) == ROLE_ENGINEER
        assert user_role({"role": ""}) == ROLE_ENGINEER
        assert user_role({}) == ROLE_ENGINEER

    def test_unknown_value_defaults_engineer(self):
        assert user_role({"role": "admin"}) == ROLE_ENGINEER
        assert user_role({"role": "OWNER"}) == ROLE_OWNER  # 大小写归一

    def test_orm_object_shape(self):
        assert user_role(User(role=ROLE_VIEWER)) == ROLE_VIEWER
        assert user_role(User(role=None)) == ROLE_ENGINEER


class TestEnsurePermission:
    def test_super_admin_bypass(self):
        ensure_permission({"super_admin_flag": 1, "role": ROLE_VIEWER}, "tasks")

    def test_viewer_403_with_role_in_message(self):
        with pytest.raises(HTTPException) as exc:
            ensure_permission({"super_admin_flag": 0, "role": ROLE_VIEWER}, "datasets")
        assert exc.value.status_code == 403
        assert "viewer" in exc.value.detail and "datasets" in exc.value.detail

    def test_engineer_and_owner_allowed(self):
        ensure_permission({"role": ROLE_ENGINEER}, "tasks")
        ensure_permission({"role": ROLE_OWNER}, "tasks")

    def test_manage_team_owner_only(self):
        ensure_permission({"role": ROLE_OWNER}, "team", action="manage")
        with pytest.raises(HTTPException):
            ensure_permission({"role": ROLE_ENGINEER}, "team", action="manage")


# ==================== 2. API 级角色闸 ====================

@pytest.fixture()
def rbac(monkeypatch):
    """平台 app + sqlite 库：同团队三角色用户 + 超管 + 跨团队 owner"""
    from app.core.config import Settings

    stub = Settings(
        _env_file=None, DATABASE_URL="postgresql://unit-test",
        AUTH_JWT_SECRET="unit-test-jwt-secret", AUTH_METHODS="local",
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: stub)
    for mod in (jwt_service, sso_service, sso_api):
        monkeypatch.setattr(mod, "get_settings", lambda: stub)

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Team.__table__, SsoOidcConfig.__table__,
        SsoTeamMapping.__table__, LdapConfig.__table__, OperationLog.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.api.user_api", "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    db = factory()
    db.add(Team(team_code="QE", team_name="质量工程"))
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, role="owner", is_active=True))
    db.add(User(user_code="u_owner", username="boss", org_code="QE", role="owner",
                password_hash="x", is_active=True))
    db.add(User(user_code="u_eng", username="eng", org_code="QE", role="engineer",
                password_hash="x", is_active=True))
    db.add(User(user_code="u_view", username="viewer", org_code="QE", role="viewer",
                password_hash="x", is_active=True))
    db.add(User(user_code="u_other_owner", username="otherboss", org_code="OTHER",
                role="owner", password_hash="x", is_active=True))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    # 受保护替身端点：模拟已接线写端点的闸行为（真实端点接线由静态断言覆盖）
    from app.services.access_control import ensure_permission as gate

    @app.post("/api/v1/datasets")
    def create_dataset(request: Request):
        gate(request.state.user, "datasets")
        return {"ok": True}

    @app.get("/api/v1/datasets")
    def list_datasets(request: Request):
        return {"ok": True, "user": request.state.user["username"]}

    app.include_router(user_api.auth_router, prefix="/api/v1")
    app.include_router(team_api.router, prefix="/api/v1")

    def token(user_code, username):
        return jwt_service.create_token(user_code, username)

    return {
        "client": TestClient(app),
        "db": db,
        "tokens": {
            "admin": token("u_admin", "admin"),
            "owner": token("u_owner", "boss"),
            "engineer": token("u_eng", "eng"),
            "viewer": token("u_view", "viewer"),
            "other_owner": token("u_other_owner", "otherboss"),
        },
    }


def fastapi_request():
    """懒取 fastapi.Request 类型（避免顶层命名遮蔽 fixture 内变量）"""
    from fastapi import Request
    return Request


class TestApiRoleGate:
    def test_viewer_write_403(self, rbac):
        resp = rbac["client"].post(
            "/api/v1/datasets",
            headers={"Authorization": f"Bearer {rbac['tokens']['viewer']}"})
        assert resp.status_code == 403
        assert "viewer" in resp.json()["detail"]

    def test_engineer_and_owner_pass_gate(self, rbac):
        for who in ("engineer", "owner", "admin"):
            resp = rbac["client"].post(
                "/api/v1/datasets",
                headers={"Authorization": f"Bearer {rbac['tokens'][who]}"})
            assert resp.status_code == 200, f"{who} 应过角色闸"

    def test_viewer_read_unaffected(self, rbac):
        resp = rbac["client"].get(
            "/api/v1/datasets",
            headers={"Authorization": f"Bearer {rbac['tokens']['viewer']}"})
        assert resp.status_code == 200

    def test_login_response_carries_role(self, rbac):
        from app.services.password_service import hash_password
        db = rbac["db"]
        db.query(User).filter(User.user_code == "u_view").update(
            {"password_hash": hash_password("pwd123")})
        db.commit()
        resp = rbac["client"].post("/api/v1/auth/login",
                                   json={"username": "viewer", "password": "pwd123"})
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "viewer"


class TestTeamRoleApi:
    def _patch_role(self, rbac, who, target="u_eng", team="QE", role="viewer"):
        return rbac["client"].patch(
            f"/api/v1/teams/{team}/members/{target}/role",
            headers={"Authorization": f"Bearer {rbac['tokens'][who]}"},
            json={"role": role})

    def test_owner_can_change_member_role_with_audit(self, rbac):
        resp = self._patch_role(rbac, "owner", target="u_eng", role="viewer")
        assert resp.status_code == 200, resp.text
        db = rbac["db"]
        db.expire_all()
        assert db.get(User, "u_eng").role == "viewer"
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "team_member_role").first()
        assert log is not None
        assert log.detail["old_role"] == "engineer" and log.detail["new_role"] == "viewer"

    def test_engineer_cannot_change_role(self, rbac):
        resp = self._patch_role(rbac, "engineer")
        assert resp.status_code == 403

    def test_viewer_cannot_change_role(self, rbac):
        resp = self._patch_role(rbac, "viewer", target="u_eng", role="owner")
        assert resp.status_code == 403

    def test_cross_team_owner_403(self, rbac):
        """owner 权限仅及本团队：跨团队 owner 403（非成员口径）"""
        resp = self._patch_role(rbac, "other_owner", target="u_eng", role="viewer")
        assert resp.status_code in (403, 404)

    def test_last_owner_cannot_be_demoted(self, rbac):
        """QE 团队仅 u_owner 一位 owner：降级 409（末位 owner 保护）"""
        resp = self._patch_role(rbac, "owner", target="u_owner", role="engineer")
        assert resp.status_code == 409

    def test_last_owner_demote_ok_after_promoting_another(self, rbac):
        """先提拔第二位 owner，再降级原 owner：放行"""
        assert self._patch_role(rbac, "owner", target="u_eng", role="owner").status_code == 200
        resp = self._patch_role(rbac, "owner", target="u_owner", role="engineer")
        assert resp.status_code == 200

    def test_super_admin_can_change_role(self, rbac):
        resp = self._patch_role(rbac, "admin", target="u_view", role="engineer")
        assert resp.status_code == 200

    def test_invalid_role_400(self, rbac):
        resp = self._patch_role(rbac, "owner", role="superadmin")
        assert resp.status_code == 400

    def test_member_list_carries_role(self, rbac):
        resp = rbac["client"].get(
            "/api/v1/teams/QE/members",
            headers={"Authorization": f"Bearer {rbac['tokens']['admin']}"})
        assert resp.status_code == 200
        roles = {item["user_code"]: item["role"] for item in resp.json()["items"]}
        assert roles["u_owner"] == "owner"
        assert roles["u_eng"] == "engineer"
        assert roles["u_view"] == "viewer"


# ==================== 3. 接线静态断言（写端点必须挂闸） ====================

class TestWriteEndpointWiring:
    """静态断言：M4 QE4-3 点名的资源写端点已挂 ensure_permission（源码级）"""

    EXPECTED = {
        "app/api/evaluation_task_api.py": 10,  # create/update/delete/run/stop+定时 get/upsert/delete（M5 QE5-3）+标注提交（M8 QE8-1）+裁判对拍（M8 QE8-2）
        "app/api/dataset_api.py": 12,          # CRUD+fields+imports+data 行级+版本打版/恢复（M5 QE5-1）
        "app/api/model_config_api.py": 3,      # create/update/delete
        "app/api/media_api.py": 2,             # upload/delete
        "app/api/instruction_config_api.py": 5,  # 模板 config C/U/D + 版本打版/恢复（M5 QE5-2）
        "app/api/webhook_api.py": 2,           # create/delete
        "app/api/api_key_api.py": 2,           # create/revoke
        "app/api/share_api.py": 2,             # 分享链接 create/revoke（M5 QE5-4）
        "app/api/ingest_api.py": 1,            # OTel/Phoenix/Langfuse trace 导入（M8 QE8-4）
    }

    def test_write_endpoints_guarded(self):
        from pathlib import Path
        for path, expected in self.EXPECTED.items():
            text = Path(path).read_text(encoding="utf-8")
            count = text.count('ensure_permission(request.state.user,')
            assert count == expected, \
                f"{path} 应有 {expected} 处 ensure_permission 接线，实际 {count}"
