"""M4 安全测试：OIDC SSO 新增对外攻击面（tests/security 纪律配套）

对象：/api/v1/auth/oidc/login、/api/v1/auth/oidc/callback（匿名白名单
端点）、/api/v1/auth/methods 与超管配置面。攻击路径复现（红→绿）：

1. 回调伪造：state 不匹配 / 伪造 tx Cookie / 过期 tx / 缺 Cookie ——
   一律错误回跳且**零开户**（callback 是唯一匿名写入口）；
2. PKCE 防 authorization code 拦截：verifier 被换（tx 篡改经签名闸
   先拦）、code 重放（IdP 一次性）、错 client_secret（invalid_grant）；
3. 授权 URL 卫生：response_type=code + S256 + state + 固定 redirect_uri；
4. 开放重定向：login?redirect=//evil / https://evil —— 回跳仅允许
   平台内相对路径；
5. 配置面越权：匿名 401（不入白名单）、普通 JWT 403、API Key 不可达
   （scope 注册表 default-deny 静态断言）；
6. 凭据不泄漏：config 读脱敏、审计 detail 无 secret、methods 不带 issuer。
"""
import base64
import hashlib
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
from urllib.parse import parse_qs, urlparse

from app.core import scopes
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping
from app.models.team_models import Team
from app.models.user_models import User
from app.services import jwt_service, sso_service
from app.api import sso_api, user_api

pytestmark = pytest.mark.unit  # W8 分类：unit（mock IdP + sqlite 替身，无真实网络）

IDP_BASE = "https://idp.test"
CLIENT_ID = "aievo-client"
CLIENT_SECRET = "idp-secret-0123456789"
CALLBACK = "/api/v1/auth/oidc/callback"


def _idp_app(secret=CLIENT_SECRET):
    idp = FastAPI()
    issued: dict = {}
    tokens: dict = {}

    @idp.get("/.well-known/openid-configuration")
    def discovery():
        return {
            "authorization_endpoint": f"{IDP_BASE}/authorize",
            "token_endpoint": f"{IDP_BASE}/token",
            "userinfo_endpoint": f"{IDP_BASE}/userinfo",
        }

    @idp.get("/authorize")
    def authorize(request: Request):
        q = request.query_params
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
        assert q["code_challenge_method"] == "S256" and q["code_challenge"]
        code = f"code-{uuid.uuid4().hex[:12]}"
        issued[code] = (q["code_challenge"], q["client_id"], q["redirect_uri"])
        return RedirectResponse(
            f"{q['redirect_uri']}?code={code}&state={q.get('state', '')}", 302)

    @idp.post("/token")
    async def token(request: Request):
        form = await request.form()
        if form.get("client_secret") != secret:
            return {"error": "invalid_client"}
        bound = issued.pop(form.get("code"), None)
        if bound is None:
            return {"error": "invalid_grant"}
        challenge, _, redirect_uri = bound
        if form.get("redirect_uri") != redirect_uri:
            return {"error": "invalid_grant"}
        calc = base64.urlsafe_b64encode(
            hashlib.sha256((form.get("code_verifier") or "").encode()).digest()
        ).rstrip(b"=").decode()
        if calc != challenge:
            return {"error": "invalid_grant"}
        access_token = f"at-{uuid.uuid4().hex[:10]}"
        tokens[access_token] = {
            "sub": "sub-1", "email": "alice@corp.test",
            "preferred_username": "alice", "name": "张三",
            "groups": ["qe"],
        }
        return {"access_token": access_token, "token_type": "Bearer"}

    @idp.get("/userinfo")
    def userinfo(request: Request):
        authz = request.headers.get("authorization", "")
        return tokens.get(authz[7:], {"error": "invalid_token"}) if authz.startswith("Bearer ") else {"error": "invalid_token"}

    return idp


@pytest.fixture()
def sec(monkeypatch):
    """平台 + mock IdP + 超管/普通用户装配（详见 tests/unit/test_oidc_sso.py 同构装配）"""
    from app.core.config import Settings

    stub = Settings(
        _env_file=None, DATABASE_URL="postgresql://unit-test",
        AUTH_JWT_SECRET="unit-test-jwt-secret", AUTH_METHODS="local,oidc",
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: stub)
    for mod in (jwt_service, sso_service, sso_api):
        monkeypatch.setattr(mod, "get_settings", lambda: stub)

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Team.__table__, SsoOidcConfig.__table__,
        SsoTeamMapping.__table__, OperationLog.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.api.user_api", "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    idp_state = {"app": _idp_app()}

    def _factory():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=idp_state["app"]), base_url=IDP_BASE)

    monkeypatch.setattr(sso_service, "_client_factory", _factory)
    sso_service._discovery_cache.clear()

    db = factory()
    db.add(SsoOidcConfig(issuer=IDP_BASE, client_id=CLIENT_ID,
                         client_secret=CLIENT_SECRET, scopes="openid profile email groups",
                         redirect_uri=f"https://aievo.test{CALLBACK}", enabled=True))
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(User(user_code="u_user", username="nobody", super_admin_flag=0, is_active=True))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(user_api.auth_router, prefix="/api/v1")
    app.include_router(sso_api.router, prefix="/api/v1")
    return {
        "client": TestClient(app, follow_redirects=False),
        "idp": TestClient(idp_state["app"], follow_redirects=False),
        "idp_state": idp_state,
        "db": db,
        "factory": factory,
        "admin_token": jwt_service.create_token("u_admin", "admin"),
        "user_token": jwt_service.create_token("u_user", "nobody"),
    }


def _begin(platform_client, idp_client):
    """SSO 前半程：login 302 → IdP authorize 302 → 返回 (回调 URL, tx cookie)"""
    resp = platform_client.get(f"/api/v1/auth/oidc/login")
    assert resp.status_code == 302
    tx = resp.cookies.get(sso_service.TX_COOKIE_NAME)
    idp_resp = idp_client.get(resp.headers["location"])
    assert idp_resp.status_code == 302
    return idp_resp.headers["location"], tx


def _assert_error_redirect(resp, db, code_value=None):
    """错误回跳三断言：302 到 /login?sso_error=、URL 不携带真实凭证值
    （code 值/签发 token 值）、零开户"""
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("/login?sso_error=")
    assert "sso_token=" not in loc
    if code_value:
        assert code_value not in loc
    assert db.query(User).filter(User.email == "alice@corp.test").count() == 0


# ---------------- 1. 回调伪造 ----------------

class TestCallbackForgery:
    def test_state_mismatch_rejected(self, sec):
        callback_url, tx = _begin(sec["client"], sec["idp"])
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, tx)
        # 篡改 state（攻击者自有 state 与受害者 Cookie 拼接）
        forged = callback_url.rsplit("state=", 1)[0] + "state=attacker-state"
        resp = sec["client"].get(forged)
        _assert_error_redirect(resp, sec["db"])

    def test_forged_tx_cookie_rejected(self, sec):
        callback_url, _ = _begin(sec["client"], sec["idp"])
        # 无有效 HMAC 签名的 tx（攻击者自造 Cookie）
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, "eyJzdGF0ZSI6ImV2aWwifQ.ZmFrZQ")
        resp = sec["client"].get(callback_url)
        _assert_error_redirect(resp, sec["db"])

    def test_tampered_tx_verifier_rejected_by_signature(self, sec):
        """改 verifier 后重拼 tx：签名闸先拦（PKCE 挑战值不可被偷换）"""
        _, tx = _begin(sec["client"], sec["idp"])
        body, sig = tx.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload["verifier"] = "attacker-verifier-attacker-verifier-attacker"
        evil = base64.urlsafe_b64encode(
            json.dumps(payload).encode()).rstrip(b"=").decode()
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, f"{evil}.{sig}")

        login_resp = sec["client"].get("/api/v1/auth/oidc/login")
        idp_resp = sec["idp"].get(login_resp.headers["location"])
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, f"{evil}.{sig}")
        resp = sec["client"].get(idp_resp.headers["location"])
        _assert_error_redirect(resp, sec["db"])

    def test_expired_tx_rejected(self, sec):
        callback_url, tx = _begin(sec["client"], sec["idp"])
        old_tx = sso_service.issue_tx_token("stale", "v", "/")
        # 用旧签名结构的过期 tx（state 对不上同样被拦；先直接验证过期语义）
        assert sso_service.verify_tx_token(old_tx) is not None
        real = time.time
        sso_service.time.time = lambda: real() + sso_service.TX_TTL_SECONDS + 1
        try:
            sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, tx)
            resp = sec["client"].get(callback_url)
        finally:
            sso_service.time.time = real
        _assert_error_redirect(resp, sec["db"])

    def test_missing_cookie_rejected(self, sec):
        callback_url, _ = _begin(sec["client"], sec["idp"])
        resp = sec["client"].get(callback_url)  # 无 tx Cookie（第三方带 code 直击）
        _assert_error_redirect(resp, sec["db"])


# ---------------- 2. code 防线 ----------------

class TestCodeDefenses:
    def test_code_replay_rejected(self, sec):
        callback_url, tx = _begin(sec["client"], sec["idp"])
        code_value = parse_qs(urlparse(callback_url).query)["code"][0]
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, tx)
        first = sec["client"].get(callback_url)
        assert first.status_code == 302 and "sso_token=" in first.headers["location"]
        # 重放同一 code：IdP 侧一次性消费 → invalid_grant → 错误回跳
        replay = sec["client"].get(callback_url)
        assert replay.status_code == 302
        assert "sso_error" in replay.headers["location"]
        assert code_value not in replay.headers["location"]
        # 且没有第二个用户/重复登录态签发痕迹（仅首次开户）
        assert sec["db"].query(User).filter(User.email == "alice@corp.test").count() == 1

    def test_wrong_client_secret_rejected(self, sec):
        """平台存错 secret（或 IdP 侧轮换）：invalid_client 路径，零开户"""
        sec["idp_state"]["app"] = _idp_app(secret="rotated-secret")
        sso_service._discovery_cache.clear()
        callback_url, tx = _begin(sec["client"], TestClient(sec["idp_state"]["app"], follow_redirects=False))
        code_value = parse_qs(urlparse(callback_url).query)["code"][0]
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, tx)
        resp = sec["client"].get(callback_url)
        _assert_error_redirect(resp, sec["db"], code_value=code_value)


# ---------------- 3. 授权 URL 卫生 ----------------

class TestAuthorizationUrlHygiene:
    def test_pkce_s256_and_state_present(self, sec):
        resp = sec["client"].get("/api/v1/auth/oidc/login")
        assert resp.status_code == 302
        url = urlparse(resp.headers["location"])
        assert url.scheme == "https" and url.netloc == "idp.test"
        q = parse_qs(url.query)
        assert q["response_type"] == ["code"]
        assert q["code_challenge_method"] == ["S256"]
        assert len(q["code_challenge"][0]) == 43  # sha256 → base64url 恰 43 字符
        assert q["state"][0]
        assert q["redirect_uri"][0].endswith(CALLBACK)


# ---------------- 4. 开放重定向 ----------------

class TestOpenRedirect:
    @pytest.mark.parametrize("evil", [
        "https://evil.example/phish",
        "//evil.example",
        "/\\evil.example",
    ])
    def test_redirect_param_sanitized(self, sec, evil):
        resp = sec["client"].get(f"/api/v1/auth/oidc/login?redirect={evil}")
        assert resp.status_code == 302
        tx = resp.cookies.get(sso_service.TX_COOKIE_NAME)
        idp_resp = sec["idp"].get(resp.headers["location"])
        sec["client"].cookies.set(sso_service.TX_COOKIE_NAME, tx)
        final = sec["client"].get(idp_resp.headers["location"])
        assert final.status_code == 302
        loc = final.headers["location"]
        assert loc.startswith("/login?sso_token=")
        # 回跳 redirect 只可能是安全相对路径（evil 被替换为 /）
        redirect_val = parse_qs(urlparse(loc).query).get("redirect", ["/"])[0]
        assert redirect_val.startswith("/") and not redirect_val.startswith("//")
        assert "evil" not in redirect_val


# ---------------- 5. 配置面越权 ----------------

class TestConfigSurfaceAccess:
    def test_anonymous_401_not_whitelisted(self, sec):
        """配置面不入中间件白名单：匿名访问 401（而非 200/403）"""
        cases = [
            ("get", "/api/v1/auth/oidc/config", None),
            ("put", "/api/v1/auth/oidc/config", {}),
            ("post", "/api/v1/auth/oidc/test", {}),
            ("get", "/api/v1/auth/oidc/team-mappings", None),
            ("post", "/api/v1/auth/oidc/team-mappings", {}),
        ]
        for method, path, body in cases:
            kwargs = {"json": body} if body is not None else {}
            resp = getattr(sec["client"], method)(path, **kwargs)
            assert resp.status_code == 401, f"{method} {path} 应 401，实际 {resp.status_code}"

    def test_non_admin_jwt_403(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['user_token']}"})
        resp = sec["client"].get("/api/v1/auth/oidc/config")
        assert resp.status_code == 403

    def test_api_key_default_deny_static(self):
        """SSO 配置面不进 API Key scope 注册表：default-deny 静态断言

        （scope 路由注册表 miss → 中间件 403「API Key 无权访问该端点」，
        CI 无需真造 Key 即可锁死「注册即放行」的回归。）
        """
        assert scopes.match_api_key_route("GET", "/api/v1/auth/oidc/config") is None
        assert scopes.match_api_key_route("PUT", "/api/v1/auth/oidc/config") is None
        assert scopes.match_api_key_route("POST", "/api/v1/auth/oidc/test") is None
        assert scopes.match_api_key_route("POST", "/api/v1/auth/oidc/team-mappings") is None


# ---------------- 6. 凭据不泄漏 ----------------

class TestSecretHygiene:
    def test_config_read_masks_secret(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['admin_token']}"})
        body = sec["client"].get("/api/v1/auth/oidc/config").json()
        assert "****" in body["config"]["client_secret"]
        assert CLIENT_SECRET not in json.dumps(body)

    def test_audit_detail_has_no_secret(self, sec):
        sec["client"].headers.update({"Authorization": f"Bearer {sec['admin_token']}"})
        sec["client"].put("/api/v1/auth/oidc/config", json={
            "issuer": IDP_BASE, "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET, "enabled": True,
        })
        logs = sec["db"].query(OperationLog).all()
        assert logs, "配置保存应落审计"
        assert CLIENT_SECRET not in json.dumps(
            [log.detail for log in logs if log.detail])

    def test_methods_endpoint_leaks_nothing(self, sec):
        body = sec["client"].get("/api/v1/auth/methods").json()
        assert set(body) == {"methods", "register_enabled",
                             "oidc_configured", "ldap_configured"}
        assert IDP_BASE not in json.dumps(body) and CLIENT_ID not in json.dumps(body)
