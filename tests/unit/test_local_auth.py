"""本地账号认证单元测试

覆盖：
- password_service：哈希/校验往返、错密码、损坏哈希
- jwt_service：签发/解析往返、错误签名、过期、缺 sub
- 注册 API：首用户自动超管、次用户普通、重名 409、注册关闭 403
- 登录 API：成功、错密码 401、禁用 403
- 中间件：白名单放行、无 token 401、有效 token 注入 request.state.user、
  禁用用户 401、Bearer 格式错误 401
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.user_models import User
from app.services import jwt_service
from app.services.password_service import hash_password, verify_password
from app.api.user_api import RegisterRequest, LoginRequest, login, register
from app.middleware.auth_middleware import AuthMiddleware

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标


# ---------------------------------------------------------------------------
# password_service
# ---------------------------------------------------------------------------

class TestPasswordService:
    def test_roundtrip(self):
        stored = hash_password("s3cret-密码")
        assert stored.startswith("pbkdf2_sha256$")
        assert verify_password("s3cret-密码", stored)

    def test_wrong_password(self):
        stored = hash_password("right")
        assert not verify_password("wrong", stored)

    def test_salt_makes_hashes_differ(self):
        assert hash_password("same") != hash_password("same")

    def test_corrupted_hash_returns_false(self):
        assert not verify_password("x", "not-a-valid-hash")
        assert not verify_password("x", "")
        assert not verify_password("x", None)
        assert not verify_password("x", "md5$abc$def$ghi")


# ---------------------------------------------------------------------------
# jwt_service
# ---------------------------------------------------------------------------

class TestJwtService:
    def test_roundtrip(self):
        token = jwt_service.create_token("u_abc", "alice")
        payload = jwt_service.decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u_abc"
        assert payload["username"] == "alice"
        assert payload["exp"] > payload["iat"]

    def test_tampered_signature_rejected(self):
        token = jwt_service.create_token("u_abc", "alice")
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        assert jwt_service.decode_token(bad) is None

    def test_wrong_secret_rejected(self, monkeypatch):
        token = jwt_service.create_token("u_abc", "alice")
        monkeypatch.setattr(
            type(jwt_service.get_settings()),
            "auth_jwt_secret",
            property(lambda self: "other-secret"),
        )
        assert jwt_service.decode_token(token) is None

    def test_expired_rejected(self, monkeypatch):
        token = jwt_service.create_token("u_abc", "alice")
        payload = jwt_service.decode_token(token)
        real_time = time.time
        monkeypatch.setattr(
            jwt_service.time, "time",
            lambda: payload["exp"] + 10)
        assert jwt_service.decode_token(token) is None
        assert real_time() > 0

    def test_missing_sub_rejected(self):
        assert jwt_service.decode_token("garbage") is None
        assert jwt_service.decode_token("") is None
        assert jwt_service.decode_token("a.b.c") is None


# ---------------------------------------------------------------------------
# 注册 / 登录 API（sqlite 内存库）
# ---------------------------------------------------------------------------

def _mem_engine():
    """内存 sqlite：StaticPool 共享单连接（跨线程/多 session 可见同一库）"""
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def db():
    engine = _mem_engine()
    Base.metadata.create_all(engine, tables=[User.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


class TestRegisterApi:
    def test_first_user_becomes_super_admin(self, db):
        result = _run(register(RegisterRequest(username="alice", password="secret1"), db))
        assert result["user"]["username"] == "alice"
        assert result["user"]["super_admin_flag"] == 1
        assert result["token"]
        row = db.query(User).filter(User.username == "alice").first()
        assert row.user_code.startswith("u_")
        assert row.password_hash

    def test_second_user_is_normal(self, db):
        _run(register(RegisterRequest(username="alice", password="secret1"), db))
        result = _run(register(RegisterRequest(username="bob", password="secret2"), db))
        assert result["user"]["super_admin_flag"] == 0

    def test_duplicate_username_409(self, db):
        _run(register(RegisterRequest(username="alice", password="secret1"), db))
        with pytest.raises(HTTPException) as exc:
            _run(register(RegisterRequest(username="alice", password="secret2"), db))
        assert exc.value.status_code == 409

    def test_register_disabled_403(self, db, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(AUTH_REGISTER_ENABLED=False))
        with pytest.raises(HTTPException) as exc:
            _run(register(RegisterRequest(username="alice", password="secret1"), db))
        assert exc.value.status_code == 403

    def test_display_name_defaults_to_username(self, db):
        result = _run(register(RegisterRequest(username="alice", password="secret1"), db))
        assert result["user"]["user_name"] == "alice"


class TestLoginApi:
    def _seed(self, db, username="alice", password="secret1", active=True):
        db.add(User(
            user_code=f"u_{username}", username=username,
            password_hash=hash_password(password), user_name=username,
            super_admin_flag=0, is_active=active,
        ))
        db.commit()

    def test_success(self, db):
        self._seed(db)
        result = _run(login(LoginRequest(username="alice", password="secret1"), db))
        assert result["user"]["username"] == "alice"
        assert result["token"]
        payload = jwt_service.decode_token(result["token"])
        assert payload["sub"] == "u_alice"

    def test_wrong_password_401(self, db):
        self._seed(db)
        with pytest.raises(HTTPException) as exc:
            _run(login(LoginRequest(username="alice", password="wrong!"), db))
        assert exc.value.status_code == 401

    def test_unknown_user_401(self, db):
        with pytest.raises(HTTPException) as exc:
            _run(login(LoginRequest(username="ghost", password="whatever"), db))
        assert exc.value.status_code == 401

    def test_inactive_user_403(self, db):
        self._seed(db, active=False)
        with pytest.raises(HTTPException) as exc:
            _run(login(LoginRequest(username="alice", password="secret1"), db))
        assert exc.value.status_code == 403

    def test_legacy_user_without_password_401(self, db):
        """存量 SSO 用户（无 password_hash）不能本地登录"""
        db.add(User(user_code="legacy", username="legacy", user_name="legacy",
                    password_hash=None, is_active=True))
        db.commit()
        with pytest.raises(HTTPException) as exc:
            _run(login(LoginRequest(username="legacy", password="anything"), db))
        assert exc.value.status_code == 401


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 鉴权中间件（裸 app + TestClient）
# ---------------------------------------------------------------------------

@pytest.fixture()
def client_factory(monkeypatch):
    def _make(users=()):
        engine = _mem_engine()
        Base.metadata.create_all(engine, tables=[User.__table__])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        s = factory()
        for u in users:
            s.add(u)
        s.commit()
        s.close()
        monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", factory)

        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/v1/protected")
        def protected(request: Request):
            return {"username": request.state.user["username"],
                    "super_admin_flag": request.state.user["super_admin_flag"]}

        @app.get("/health")
        def health():
            return {"ok": True}

        @app.get("/api/v1/auth/login")
        def fake_login():
            return {"ok": True}

        return TestClient(app, raise_server_exceptions=False)

    return _make


def _active_user(username="alice", password="secret1", active=True, super_admin=0):
    return User(
        user_code=f"u_{username}", username=username,
        password_hash=hash_password(password), user_name=username,
        super_admin_flag=super_admin, is_active=active,
    )


class TestAuthMiddleware:
    def test_whitelist_passthrough(self, client_factory):
        client = client_factory()
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/auth/login").status_code == 200
        assert client.get("/api/v1/auth/register").status_code == 404  # 已过中间件，404 来自路由未注册
        assert client.get("/").status_code == 404  # 根路径白名单，404 来自路由缺失而非 401
        assert client.get("/login").status_code == 404  # 非 API 路径放行（SPA 前端路由由 catch-all 托管）

    def test_missing_token_401(self, client_factory):
        client = client_factory(users=[_active_user()])
        resp = client.get("/api/v1/protected")
        assert resp.status_code == 401
        assert resp.json()["code"] == 401

    def test_valid_token_sets_state_user(self, client_factory):
        client = client_factory(users=[_active_user(super_admin=1)])
        token = jwt_service.create_token("u_alice", "alice")
        resp = client.get("/api/v1/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json() == {"username": "alice", "super_admin_flag": 1}

    def test_invalid_token_401(self, client_factory):
        client = client_factory(users=[_active_user()])
        resp = client.get("/api/v1/protected", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401

    def test_wrong_scheme_401(self, client_factory):
        client = client_factory(users=[_active_user()])
        token = jwt_service.create_token("u_alice", "alice")
        resp = client.get("/api/v1/protected", headers={"Authorization": f"Basic {token}"})
        assert resp.status_code == 401

    def test_inactive_user_403(self, client_factory):
        client = client_factory(users=[_active_user(active=False)])
        token = jwt_service.create_token("u_alice", "alice")
        resp = client.get("/api/v1/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_unknown_user_401(self, client_factory):
        client = client_factory()
        token = jwt_service.create_token("u_ghost", "ghost")
        resp = client.get("/api/v1/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
