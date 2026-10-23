"""OIDC SSO 单元测试（M4 QE4-1）：mock IdP 全流程

mock IdP 是进程内 FastAPI 应用（httpx.ASGITransport 挂载，零真实网络），
实现标准 OIDC 授权码 + PKCE（S256）三端点：

- GET /.well-known/openid-configuration —— discovery 文档
- GET /authorize —— 校验 client_id/redirect_uri/state/code_challenge（S256），
  签发一次性 code 并绑定 challenge/redirect_uri，302 回平台回调
- POST /token —— 校验 code/client 凭证/redirect_uri/code_verifier（重算
  S256 challenge 与 authorize 侧绑定值比对），返回 access_token
- GET /userinfo —— Bearer access_token → claims（sub/email/
  preferred_username/name/groups）

覆盖：
- 全流程：login 302 → IdP authorize → callback → JIT 开户 + groups→team
  映射 + 签发本站 JWT（/login?sso_token= 回跳）
- JIT 与既有账号 email 映射（大小写不敏感）、用户名防撞
- groups 未命中映射不动归属、命中即时建团（upsert）
- AUTH_METHODS 开关：纯 local 时 SSO 403、纯 oidc 时本地登录/注册 403
- /auth/methods 公告（local/oidc/register_enabled/oidc_configured）
- 配置面：脱敏回显、secret 保留语义、连通性自检、映射 CRUD 与审计留痕
"""
import base64
import hashlib
import hmac as hmac_mod
import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping
from app.models.team_models import Team
from app.models.user_models import User
from app.services import jwt_service, sso_service
from app.api import sso_api, user_api

pytestmark = pytest.mark.unit  # W8 分类门禁：全部替身（mock IdP + sqlite 内存库）


# ==================== mock IdP（进程内，零真实网络） ====================

IDP_BASE = "https://idp.test"
IDP_CLIENT_ID = "aievo-client"
IDP_CLIENT_SECRET = "idp-secret-0123456789"
CALLBACK_PATH = "/api/v1/auth/oidc/callback"

IDP_CLAIMS = {
    "sub": "idp-sub-001",
    "email": "Alice@Corp.Test",
    "preferred_username": "alice",
    "name": "张三",
    "groups": ["qe-engineers", "everyone"],
}


def _make_idp_app(claims_overrides=None):
    """构造 mock IdP：授权码 + PKCE（S256）三端点，code 一次性绑定"""
    idp = FastAPI()
    claims = {**IDP_CLAIMS, **(claims_overrides or {})}
    # code → (challenge, client_id, redirect_uri)；一次性消费
    issued_codes: dict = {}
    tokens: dict = {}

    @idp.get("/.well-known/openid-configuration")
    def discovery():
        return {
            "issuer": IDP_BASE,
            "authorization_endpoint": f"{IDP_BASE}/authorize",
            "token_endpoint": f"{IDP_BASE}/token",
            "userinfo_endpoint": f"{IDP_BASE}/userinfo",
        }

    @idp.get("/authorize")
    def authorize(request: Request):
        q = request.query_params
        assert q.get("response_type") == "code", "response_type 必须为 code"
        assert q.get("client_id") == IDP_CLIENT_ID, "client_id 不匹配"
        assert q.get("code_challenge_method") == "S256", "PKCE 必须 S256"
        assert q.get("code_challenge"), "缺少 code_challenge"
        assert "openid" in (q.get("scope") or "").split(), "scope 须含 openid"
        redirect_uri = q.get("redirect_uri")
        assert redirect_uri and redirect_uri.endswith(CALLBACK_PATH), f"redirect_uri 非平台回调: {redirect_uri}"
        code = f"code-{uuid.uuid4().hex[:12]}"
        issued_codes[code] = (q.get("code_challenge"), q.get("client_id"), redirect_uri)
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            f"{redirect_uri}{sep}code={code}&state={q.get('state', '')}", status_code=302)

    @idp.post("/token")
    async def token(request: Request):
        form = await request.form()
        if form.get("client_id") != IDP_CLIENT_ID or form.get("client_secret") != IDP_CLIENT_SECRET:
            return {"error": "invalid_client"}
        code = form.get("code")
        bound = issued_codes.pop(code, None)
        if bound is None:
            return {"error": "invalid_grant"}
        challenge, client_id, redirect_uri = bound
        if form.get("client_id") != client_id or form.get("redirect_uri") != redirect_uri:
            return {"error": "invalid_grant"}
        verifier = form.get("code_verifier") or ""
        calc = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        if not hmac_mod.compare_digest(calc, challenge):
            return {"error": "invalid_grant"}
        access_token = f"at-{uuid.uuid4().hex[:12]}"
        tokens[access_token] = claims
        return {"access_token": access_token, "token_type": "Bearer", "expires_in": 3600}

    @idp.get("/userinfo")
    def userinfo(request: Request):
        authz = request.headers.get("authorization", "")
        if not authz.startswith("Bearer ") or authz[7:] not in tokens:
            return {"error": "invalid_token"}
        return tokens[authz[7:]]

    return idp


# ==================== 平台测试装配件 ====================

def _settings_stub(monkeypatch, auth_methods="local,oidc", register_enabled=True):
    """密闭 Settings（固定 JWT 密钥 + 指定 AUTH_METHODS）——沿用
    test_local_auth 的 _hermetic_jwt_secret 口径，脱离所在机器 .env。

    打两层补丁：app.core.config.get_settings（覆盖 user_api 等**函数内**
    from-import 的取值）+ 各模块顶层已绑定的名字（jwt_service /
    sso_service / sso_api 的 from ... import get_settings）。
    """
    from app.core.config import Settings, get_settings as _orig

    stub = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://unit-test",
        AUTH_JWT_SECRET="unit-test-jwt-secret",
        AUTH_REGISTER_ENABLED=register_enabled,
        AUTH_METHODS=auth_methods,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: stub)
    for mod in (jwt_service, sso_service, sso_api):
        monkeypatch.setattr(mod, "get_settings", lambda: stub)
    assert _orig is not None
    return stub


@pytest.fixture()
def platform(monkeypatch):
    """平台 app + sqlite 内存库 + mock IdP httpx 注入缝

    返回 dict：client（平台 TestClient）、db（SessionFactory）、idp
    （mock IdP TestClient，用于手动驱动 authorize）、idp_state（可变持有
    器——自定义 claims 的用例替换 state["app"] 即生效）。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        User.__table__, Team.__table__, SsoOidcConfig.__table__,
        SsoTeamMapping.__table__, OperationLog.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Depends(get_db) 内部取 app.db.session.SessionLocal；直接调用
    # SessionLocal 的模块（中间件 / user_api._auth_user_dict / audit）逐一替换
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.api.user_api", "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    idp_state = {"app": _make_idp_app()}

    # httpx 注入缝：平台服务对 IdP 的三次出网全部走进程内 ASGITransport
    def _fake_factory():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=idp_state["app"]), base_url=IDP_BASE)

    monkeypatch.setattr(sso_service, "_client_factory", _fake_factory)
    # 每个用例独立的 discovery 缓存起点（缓存属模块级全局）
    sso_service._discovery_cache.clear()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(user_api.auth_router, prefix="/api/v1")
    app.include_router(sso_api.router, prefix="/api/v1")
    return {
        "client": TestClient(app, follow_redirects=False),
        "idp": TestClient(idp_state["app"], follow_redirects=False),
        "idp_state": idp_state,
        "db": factory,
    }


def _swap_idp(platform, claims_overrides=None):
    """替换 mock IdP 的 claims（自定义 claims / 异常 claims 用例）"""
    platform["idp_state"]["app"] = _make_idp_app(claims_overrides)
    platform["idp"] = TestClient(platform["idp_state"]["app"], follow_redirects=False)
    sso_service._discovery_cache.clear()


def _seed_config(db, **overrides):
    row = SsoOidcConfig(
        issuer=IDP_BASE,
        client_id=IDP_CLIENT_ID,
        client_secret=IDP_CLIENT_SECRET,
        scopes="openid profile email groups",
        redirect_uri=f"https://aievo.test{CALLBACK_PATH}",
        enabled=True,
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return row


def _sso_flow(platform_client, idp_client):
    """驱动完整 SSO 流：返回 (平台回调响应, tx cookie)"""
    # 1. 平台 login → 302 到 IdP authorize，并种 tx cookie
    resp = platform_client.get("/api/v1/auth/oidc/login")
    assert resp.status_code == 302, resp.text
    auth_url = resp.headers["location"]
    tx_cookie = resp.cookies.get(sso_service.TX_COOKIE_NAME)
    assert tx_cookie, "login 响应应种登录事务 Cookie"

    # 2. 浏览器跳 IdP authorize → 302 回平台回调（带 code+state）
    idp_resp = idp_client.get(auth_url)
    assert idp_resp.status_code == 302, idp_resp.text
    callback_url = idp_resp.headers["location"]

    # 3. 平台回调（带 tx cookie）
    platform_client.cookies.set(sso_service.TX_COOKIE_NAME, tx_cookie)
    return platform_client.get(callback_url)


# ==================== 全流程：JIT 开户 + team 映射 + JWT ====================

class TestSsoFullFlow:
    def test_jit_provision_team_mapping_and_jwt(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(SsoTeamMapping(idp_group="qe-engineers", team_code="QE-TEAM", team_name="质量工程组"))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("/login?sso_token="), location

        # JIT 开户：email 归一小写、无本地密码、非超管、姓名取 claims
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user is not None, "应按 email JIT 开户"
        assert user.password_hash is None
        assert user.super_admin_flag == 0
        assert user.user_name == "张三"
        assert user.username == "alice"
        # groups→team 映射：即时建团 + 归属
        assert user.org_code == "QE-TEAM"
        team = db.get(Team, "QE-TEAM")
        assert team is not None and team.team_name == "质量工程组"

        # 签发的是本站 JWT（可解出 sub 且用户可加载）
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(location).query)["sso_token"][0]
        payload = jwt_service.decode_token(token)
        assert payload["sub"] == user.user_code
        # JWT 即刻可用：走一次受保护端点（白名单外需要 Bearer）
        platform["client"].headers.update({"Authorization": f"Bearer {token}"})
        status_resp = platform["client"].get("/api/v1/auth/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["is_authenticated"] is True

        # 审计：JIT 开户 CREATE user（via oidc）
        logs = db.query(OperationLog).filter(
            OperationLog.resource_type == "user").all()
        assert logs and logs[0].action == "CREATE"
        assert logs[0].detail.get("via") == "oidc"

    def test_second_login_maps_existing_user_not_duplicate(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        for _ in range(2):
            resp = _sso_flow(platform["client"], platform["idp"])
            assert resp.status_code == 302
            assert resp.headers["location"].startswith("/login?sso_token=")
            platform["client"].cookies.clear()
        assert db.query(User).filter(User.email == "alice@corp.test").count() == 1

    def test_maps_existing_account_by_email_case_insensitive(self, platform, monkeypatch):
        """既有账号 email 大小写不一致仍映射同一账号（JIT 不另开新号）"""
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(User(
            user_code="u_existing", username="alice_existing", user_name="旧名",
            email="alice@corp.test", super_admin_flag=0, is_active=True,
        ))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        users = db.query(User).filter(User.email.contains("alice")).all()
        assert len(users) == 1 and users[0].user_code == "u_existing"
        assert users[0].user_name == "张三"  # 显示名按 claims 刷新
        assert users[0].last_login_at is not None

    def test_username_collision_gets_suffix(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(User(user_code="u_x", username="alice", user_name="占位",
                    email="other@corp.test", is_active=True))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user.username != "alice"  # 防撞后缀
        assert user.username.startswith("alice")

    def test_group_without_mapping_keeps_default(self, platform, monkeypatch):
        """groups 无命中映射：不动归属（新用户 org_code 为空，走 DEFAULT 兜底）"""
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(SsoTeamMapping(idp_group="other-group", team_code="OTHER"))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user.org_code in (None, "")

    def test_first_matching_mapping_wins(self, platform, monkeypatch):
        """多组命中时按映射 id 升序取首条（确定性）"""
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(SsoTeamMapping(idp_group="everyone", team_code="ALL-TEAM", team_name="全员"))
        db.add(SsoTeamMapping(idp_group="qe-engineers", team_code="QE-TEAM", team_name="质量工程组"))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user.org_code == "ALL-TEAM"  # everyone 先建（id 更小）

    def test_custom_claims_mapping(self, platform, monkeypatch):
        """claims 字段名映射：非标准字段名（corp_mail 等）也能取到"""
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(
            db,
            claim_email="corp_mail", claim_username="uid",
            claim_name="display", claim_groups="dept",
        )
        db.commit()
        _swap_idp(platform, claims_overrides={
            "corp_mail": "bob@corp.test", "uid": "bob", "display": "李四",
            "dept": ["dept-a"],
        })

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        user = db.query(User).filter(User.email == "bob@corp.test").first()
        assert user is not None and user.username == "bob" and user.user_name == "李四"

    def test_missing_email_claim_redirects_error(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.commit()
        _swap_idp(platform, claims_overrides={"email": ""})

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        assert "sso_error" in resp.headers["location"]
        assert db.query(User).count() == 0, "失败流程不得开户"

    def test_disabled_existing_account_rejected(self, platform, monkeypatch):
        """本地禁用的账号 SSO 通道同样拒绝（与 /auth/login 同口径）"""
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.add(User(user_code="u_off", username="off", email="alice@corp.test",
                    is_active=False))
        db.commit()

        resp = _sso_flow(platform["client"], platform["idp"])
        assert resp.status_code == 302
        assert "sso_error" in resp.headers["location"]


# ==================== tx Cookie / state 防护（安全语义在 tests/security 深挖） ====================

class TestTxSession:
    def test_callback_without_tx_cookie_rejected(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.commit()
        resp = platform["client"].get(
            f"{CALLBACK_PATH}?code=x&state=y")
        assert resp.status_code == 302 and "sso_error" in resp.headers["location"]
        assert db.query(User).count() == 0


# ==================== AUTH_METHODS 开关 ====================

class TestAuthMethodsSwitch:
    def test_methods_endpoint_local_default(self, platform, monkeypatch):
        _settings_stub(monkeypatch, auth_methods="local")
        resp = platform["client"].get("/api/v1/auth/methods")
        assert resp.status_code == 200
        body = resp.json()
        assert body["methods"] == ["local"]
        assert body["oidc_configured"] is False
        assert body["register_enabled"] is True

    def test_methods_endpoint_oidc_configured(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        db = platform["db"]()
        _seed_config(db)
        db.commit()
        resp = platform["client"].get("/api/v1/auth/methods")
        assert resp.json() == {
            "methods": ["local", "oidc"], "register_enabled": True,
            "oidc_configured": True, "ldap_configured": False,
        }

    def test_local_only_blocks_sso_login(self, platform, monkeypatch):
        _settings_stub(monkeypatch, auth_methods="local")
        db = platform["db"]()
        _seed_config(db)
        db.commit()
        resp = platform["client"].get("/api/v1/auth/oidc/login")
        assert resp.status_code == 403
        assert "AUTH_METHODS" in resp.json()["detail"]

    def test_oidc_only_blocks_local_login_and_register(self, platform, monkeypatch):
        _settings_stub(monkeypatch, auth_methods="oidc")
        login = platform["client"].post(
            "/api/v1/auth/login", json={"username": "a", "password": "b"})
        assert login.status_code == 403
        reg = platform["client"].post(
            "/api/v1/auth/register",
            json={"username": "newbie", "password": "secret1"})
        assert reg.status_code == 403
        # 公告面：register_enabled=False（登录页隐藏注册 Tab 与账密表单）
        methods = platform["client"].get("/api/v1/auth/methods").json()
        assert methods == {
            "methods": ["oidc"], "register_enabled": False,
            "oidc_configured": False, "ldap_configured": False,
        }

    def test_sso_not_configured_403(self, platform, monkeypatch):
        _settings_stub(monkeypatch, auth_methods="oidc")
        resp = platform["client"].get("/api/v1/auth/oidc/login")
        assert resp.status_code == 403
        assert "未配置" in resp.json()["detail"]


# ==================== PKCE / tx 令牌单元 ====================

class TestPkceAndTxToken:
    def test_pkce_pair_shape(self):
        verifier, challenge = sso_service.generate_pkce_pair()
        assert 43 <= len(verifier) <= 128
        calc = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        assert challenge == calc

    def test_tx_token_roundtrip_and_tamper(self, monkeypatch):
        _settings_stub(monkeypatch)
        tx = sso_service.issue_tx_token("st", "vf", "/tasks")
        payload = sso_service.verify_tx_token(tx)
        assert payload["state"] == "st" and payload["verifier"] == "vf"
        assert payload["redirect"] == "/tasks"
        # 篡改 payload（换 verifier）后签名失配
        body, sig = tx.split(".")
        forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        forged["verifier"] = "evil"
        evil_body = base64.urlsafe_b64encode(
            json.dumps(forged).encode()).rstrip(b"=").decode("ascii")
        assert sso_service.verify_tx_token(f"{evil_body}.{sig}") is None

    def test_tx_token_expired(self, platform, monkeypatch):
        _settings_stub(monkeypatch)
        tx = sso_service.issue_tx_token("st", "vf", "/")
        real_time = sso_service.time.time
        monkeypatch.setattr(
            sso_service.time, "time",
            lambda: real_time() + sso_service.TX_TTL_SECONDS + 1)
        assert sso_service.verify_tx_token(tx) is None


# ==================== 超管配置面 ====================

def _admin_client(platform, monkeypatch):
    """以超管身份驱动平台（真实 JWT + 中间件链）"""
    _settings_stub(monkeypatch)
    db = platform["db"]()
    db.add(User(user_code="u_admin", username="admin", user_name="管理员",
                super_admin_flag=1, is_active=True))
    db.commit()
    token = jwt_service.create_token("u_admin", "admin")
    platform["client"].headers.update({"Authorization": f"Bearer {token}"})
    return db


class TestConfigApi:
    def test_get_config_masked(self, platform, monkeypatch):
        db = _admin_client(platform, monkeypatch)
        _seed_config(db)
        resp = platform["client"].get("/api/v1/auth/oidc/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert "****" in body["config"]["client_secret"]
        assert IDP_CLIENT_SECRET not in json.dumps(body)

    def test_get_config_unconfigured(self, platform, monkeypatch):
        _admin_client(platform, monkeypatch)
        resp = platform["client"].get("/api/v1/auth/oidc/config")
        assert resp.json() == {"configured": False, "config": None}

    def test_put_config_save_and_keep_secret(self, platform, monkeypatch):
        db = _admin_client(platform, monkeypatch)
        _seed_config(db)
        # 第一次：改 issuer，secret 传脱敏回显值 → 保留原值（连通性自检仍过）
        resp = platform["client"].put("/api/v1/auth/oidc/config", json={
            "issuer": IDP_BASE, "client_id": IDP_CLIENT_ID,
            "client_secret": "idp-****6789",
            "scopes": "openid profile email groups", "enabled": True,
        })
        assert resp.status_code == 200, resp.text
        row = sso_service.get_active_config(db)
        assert row.client_secret == IDP_CLIENT_SECRET
        # 审计落痕（不含 secret）
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "sso_config").first()
        assert log is not None and log.action == "UPDATE"

    def test_put_config_first_time_requires_secret(self, platform, monkeypatch):
        _admin_client(platform, monkeypatch)
        resp = platform["client"].put("/api/v1/auth/oidc/config", json={
            "issuer": IDP_BASE, "client_id": IDP_CLIENT_ID,
            "client_secret": "", "enabled": True,
        })
        assert resp.status_code == 400
        assert "client_secret" in resp.json()["detail"]

    def test_put_config_rejects_bad_scopes(self, platform, monkeypatch):
        _admin_client(platform, monkeypatch)
        resp = platform["client"].put("/api/v1/auth/oidc/config", json={
            "issuer": IDP_BASE, "client_id": "c", "client_secret": "s",
            "scopes": "profile email", "enabled": True,
        })
        assert resp.status_code == 400 and "openid" in resp.json()["detail"]

    def test_connectivity_test_ok(self, platform, monkeypatch):
        _admin_client(platform, monkeypatch)
        resp = platform["client"].post("/api/v1/auth/oidc/test", json={
            "issuer": IDP_BASE, "client_id": IDP_CLIENT_ID,
            "client_secret": IDP_CLIENT_SECRET, "enabled": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["endpoints"]["authorization_endpoint"] == f"{IDP_BASE}/authorize"
        assert body["latency_ms"] >= 0

    def test_connectivity_test_unreachable(self, platform, monkeypatch):
        """issuer 不可达：自检明确失败而非抛 500（超管排障入口）"""
        _admin_client(platform, monkeypatch)

        def _dead_factory():
            def _boom(*a, **k):
                raise httpx.ConnectError("no route")
            client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
            return client

        monkeypatch.setattr(sso_service, "_client_factory", _dead_factory)
        resp = platform["client"].post("/api/v1/auth/oidc/test", json={
            "issuer": "https://dead.test", "client_id": "c", "enabled": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False and body["error"]


class TestTeamMappingApi:
    def test_crud_and_audit(self, platform, monkeypatch):
        db = _admin_client(platform, monkeypatch)
        resp = platform["client"].post("/api/v1/auth/oidc/team-mappings", json={
            "idp_group": "qe-engineers", "team_code": "QE-TEAM", "team_name": "质量工程组",
        })
        assert resp.status_code == 200
        mapping_id = resp.json()["id"]

        # 重复组名 409
        dup = platform["client"].post("/api/v1/auth/oidc/team-mappings", json={
            "idp_group": "qe-engineers", "team_code": "OTHER",
        })
        assert dup.status_code == 409

        # 列表
        lst = platform["client"].get("/api/v1/auth/oidc/team-mappings")
        assert lst.status_code == 200 and len(lst.json()) == 1

        # 删除 + 审计
        dele = platform["client"].delete(f"/api/v1/auth/oidc/team-mappings/{mapping_id}")
        assert dele.status_code == 200
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "sso_team_mapping",
            OperationLog.action == "DELETE",
        ).first()
        assert log is not None
        assert platform["client"].get("/api/v1/auth/oidc/team-mappings").json() == []
