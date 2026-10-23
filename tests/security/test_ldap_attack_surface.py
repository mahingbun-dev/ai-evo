"""M4 安全测试：LDAP/AD 只读认证攻击面（tests/security 纪律配套）

对象：POST /auth/login 的 LDAP 旁路（user_api._ldap_login_fallback +
ldap_service.authenticate）与超管 /auth/ldap/* 配置面。攻击路径复现（红→绿）：

1. LDAP 滤值注入：用户名携带 filter 元字符（`*` / `(` / `)` / `\` / NUL /
   组合 `*)(objectClass=*)`）——转义后进目录（原样元字符不得出现），
   认证一律失败且零开户（过滤器语义不可被改写）；
2. 空密码匿名绑定：LDAP 服务器对空密码可能按匿名绑定成功处理——
   前置直拒（不发起任何 bind/search），401 且零开户；
3. 目录故障不外泄：服务账号密码错/目录不可达等旁路失败统一 401
   「用户名或密码错误」（不向未认证方暴露目录状态），密码不进日志；
4. 配置面越权：匿名 401（不入白名单）、普通 JWT 403、API Key 不可达
   （scope 注册表 default-deny 静态断言）；
5. 凭据不泄漏：config 读脱敏、审计 detail 无 bind_password、
   /auth/methods 不带主机/账号信息。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import scopes
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping, LdapConfig
from app.models.team_models import Team
from app.models.user_models import User
from app.services import jwt_service, ldap_service, sso_service
from app.api import sso_api, user_api

from tests.unit.test_ldap_auth import (
    FakeDirectory, SVC_DN, SVC_PWD, BASE_DN, _directory,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（内嵌 mock 目录 + sqlite 替身）


@pytest.fixture()
def sec(monkeypatch):
    from app.core.config import Settings

    stub = Settings(
        _env_file=None, DATABASE_URL="postgresql://unit-test",
        AUTH_JWT_SECRET="unit-test-jwt-secret", AUTH_METHODS="local,ldap",
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

    directory = _directory()
    monkeypatch.setattr(ldap_service, "_make_connection", directory.make_connection)

    db = factory()
    db.add(LdapConfig(
        host="ldap.corp.test", bind_dn=SVC_DN, bind_password=SVC_PWD,
        base_dn=BASE_DN,
        user_filter="(&(objectClass=person)(sAMAccountName={username}))",
        enabled=True))
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(User(user_code="u_user", username="nobody", super_admin_flag=0, is_active=True))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(user_api.auth_router, prefix="/api/v1")
    app.include_router(sso_api.router, prefix="/api/v1")
    return {
        "client": TestClient(app, follow_redirects=False),
        "db": db,
        "dir": directory,
        "admin_token": jwt_service.create_token("u_admin", "admin"),
        "user_token": jwt_service.create_token("u_user", "nobody"),
    }


def _login(sec, username, password):
    return sec["client"].post("/api/v1/auth/login",
                              json={"username": username, "password": password})


def _assert_401_no_provision(sec, resp):
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"
    assert sec["db"].query(User).filter(User.username != "admin").filter(
        User.username != "nobody").count() == 0


# ---------------- 1. 滤值注入 ----------------

class TestFilterInjection:
    @pytest.mark.parametrize("payload", [
        "*",
        "alice*",
        "*)(objectClass=*)",
        ")(|(",
        "a*b(c)d\\e",
        "a\x00b",
        "*)(sAMAccountName=alice",
    ])
    def test_metacharacters_escaped_and_rejected(self, sec, payload):
        sec["dir"].search_calls.clear()
        resp = _login(sec, payload, "whatever-password")
        _assert_401_no_provision(sec, resp)

        # 目录收到的 filter：用户名部分必须整体转义（无原样元字符）
        assert sec["dir"].search_calls, "应发起目录搜索"
        sent = sec["dir"].search_calls[-1][1]
        escaped = ldap_service.escape_filter_value(payload)
        assert escaped in sent, f"转义值未进 filter：{sent}"
        # filter 中不得出现未转义的注入原样（转义形态 \\2a/\\28/\\29/\\5c 除外）
        for raw in ("*)(", "\\e", "(c)"):
            assert raw not in sent.replace("\\2a", "").replace("\\28", "") \
                .replace("\\29", "").replace("\\5c", "").replace("\\00", ""), \
                f"filter 含未转义原样片段 {raw!r}: {sent}"

    def test_escaped_filter_cannot_widen_match(self, sec):
        """通配注入不放大命中：`a*` 转义后按字面量匹配，目录 0 命中 → 401"""
        resp = _login(sec, "a*", "pwd")
        _assert_401_no_provision(sec, resp)


# ---------------- 2. 空密码匿名绑定 ----------------

class TestEmptyPassword:
    def test_empty_password_rejected_before_any_bind(self, sec):
        """空密码两层闸：请求 schema（min_length=1）先 422 拒收；即便绕过
        schema（服务层直调，见 test_service_unit_rejects_empty），authenticate
        也前置直拒——目录侧「空密码=匿名绑定成功」的怪癖两头都堵死。"""
        sec["dir"].search_calls.clear()
        resp = _login(sec, "alice", "")
        assert resp.status_code == 422  # schema 闸（比 401 更早，语义同样是拒收）
        # 关键性质：不发起任何目录往返
        assert sec["dir"].search_calls == []

    def test_service_unit_rejects_empty(self):
        from app.services.ldap_service import LdapError
        config = LdapConfig(host="x", base_dn="y",
                            user_filter="(sAMAccountName={username})")
        with pytest.raises(LdapError):
            ldap_service.authenticate(config, "alice", "")
        with pytest.raises(LdapError):
            ldap_service.authenticate(config, "", "pwd")


# ---------------- 3. 目录故障不外泄 ----------------

class TestDirectoryFailureSilence:
    def test_wrong_service_password_uniform_401(self, sec, monkeypatch):
        """服务账号密码错（目录侧配置错）：对外仍是统一 401 文案"""
        db = sec["db"]
        row = db.query(LdapConfig).first()
        row.bind_password = "rotated"
        db.commit()
        resp = _login(sec, "alice", "alice-pwd-123")
        _assert_401_no_provision(sec, resp)

    def test_directory_down_uniform_401(self, sec, monkeypatch):
        """目录不可达（连接异常）：对外仍是统一 401，不暴露目录状态"""

        def _dead(*a, **k):
            class Dead:
                def bind(self):
                    raise OSError("connection refused")
                def unbind(self):
                    pass
            return Dead()

        monkeypatch.setattr(ldap_service, "_make_connection", _dead)
        resp = _login(sec, "alice", "alice-pwd-123")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"


# ---------------- 4. 配置面越权 ----------------

class TestLdapConfigSurfaceAccess:
    def test_anonymous_401_not_whitelisted(self, sec):
        cases = [
            ("get", "/api/v1/auth/ldap/config", None),
            ("put", "/api/v1/auth/ldap/config", {}),
            ("post", "/api/v1/auth/ldap/test", {}),
        ]
        for method, path, body in cases:
            kwargs = {"json": body} if body is not None else {}
            resp = getattr(sec["client"], method)(path, **kwargs)
            assert resp.status_code == 401, f"{method} {path} 应 401，实际 {resp.status_code}"

    def test_non_admin_jwt_403(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['user_token']}"})
        assert sec["client"].get("/api/v1/auth/ldap/config").status_code == 403

    def test_api_key_default_deny_static(self):
        """LDAP 配置面不进 API Key scope 注册表：default-deny 静态断言"""
        assert scopes.match_api_key_route("GET", "/api/v1/auth/ldap/config") is None
        assert scopes.match_api_key_route("PUT", "/api/v1/auth/ldap/config") is None
        assert scopes.match_api_key_route("POST", "/api/v1/auth/ldap/test") is None


# ---------------- 5. 凭据不泄漏 ----------------

class TestSecretHygiene:
    def test_config_read_masks_password(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['admin_token']}"})
        body = sec["client"].get("/api/v1/auth/ldap/config").json()
        assert "****" in body["config"]["bind_password"]
        assert SVC_PWD not in json.dumps(body)

    def test_audit_detail_has_no_password(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['admin_token']}"})
        sec["client"].put("/api/v1/auth/ldap/config", json={
            "host": "ldap.corp.test", "bind_dn": SVC_DN, "bind_password": SVC_PWD,
            "base_dn": BASE_DN, "enabled": True,
        })
        logs = sec["db"].query(OperationLog).filter(
            OperationLog.resource_type == "ldap_config").all()
        assert logs, "配置保存应落审计"
        assert SVC_PWD not in json.dumps([log.detail for log in logs if log.detail])

    def test_login_response_and_audit_no_password(self, sec):
        """登录成功响应/审计不携带 LDAP 密码（密码只进二次绑定连接）"""
        resp = _login(sec, "alice", "alice-pwd-123")
        assert resp.status_code == 200
        assert "alice-pwd-123" not in json.dumps(resp.json())
        logs = sec["db"].query(OperationLog).all()
        assert "alice-pwd-123" not in json.dumps(
            [log.detail for log in logs if log.detail])

    def test_methods_endpoint_leaks_nothing(self, sec):
        body = sec["client"].get("/api/v1/auth/methods").json()
        assert set(body) == {"methods", "register_enabled",
                             "oidc_configured", "ldap_configured"}
        assert SVC_DN not in json.dumps(body) and "ldap.corp.test" not in json.dumps(body)
