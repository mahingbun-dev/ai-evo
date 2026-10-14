"""auth/status 状态语义单元测试（W13，本地 Bearer 形态）

状态如实化后的行为契约：
  - 未登录（无 Authorization）→ 401 + is_authenticated=false
  - token 无效 / 用户不存在或已禁用 → 401 + is_authenticated=false
  - 已登录（有效 Bearer）→ 200 + is_authenticated=true（向后兼容字段保留）
"""
import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.user_models import User
from app.services import jwt_service
from app.services.jwt_service import create_token
from app.api import user_api
from app.api.user_api import auth_router


@pytest.fixture(autouse=True)
def _hermetic_jwt_secret(monkeypatch):
    """固定显式 JWT 密钥，脱离所在机器的 .env（ARCH-0-2）。

    JWT 密钥不再默认从 DATABASE_URL 派生（缺密钥即抛错），本文件多处调真实
    create_token 签发——CI 干净环境（无 .env）会必红，统一 patch 为显式
    密钥保持密闭。
    """
    from app.core.config import Settings
    monkeypatch.setattr(
        jwt_service, "get_settings",
        lambda: Settings(
            _env_file=None,
            DATABASE_URL="postgresql://unit-test",
            AUTH_JWT_SECRET="unit-test-jwt-secret",
        ),
    )


@pytest.fixture
def client(monkeypatch):
    """内存 SQLite + 注入 user_api.SessionLocal（端点自查 Bearer 时加载用户）"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[User.__table__])
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(user_api, "SessionLocal", TestSession)

    app = FastAPI()
    app.include_router(auth_router)
    return TestClient(app)


@pytest.fixture
def active_user(client):
    from sqlalchemy.orm import sessionmaker as sm

    # 直接复用 client 里 patch 过的 session 工厂造数据
    pass


def _make_user(monkeypatch, user_code="u1", is_active=True, super_admin_flag=0):
    """在 patch 后的 SessionLocal 里建用户，返回工厂（供用例自建数据）"""
    # 由 client fixture 已 patch user_api.SessionLocal；这里再拿一份
    from app.api import user_api as ua

    with ua.SessionLocal() as db:
        db.add(User(
            user_code=user_code,
            username=user_code,
            user_name=f"{user_code}-name",
            org_code="DEFAULT",
            super_admin_flag=super_admin_flag,
            is_active=is_active,
        ))
        db.commit()


def test_no_auth_header_returns_401(client):
    resp = client.get("/auth/status")
    assert resp.status_code == 401
    body = resp.json()
    assert body["is_authenticated"] is False
    assert body["user"] is None
    assert body["message"] == "未提供认证信息或登录已失效"


def test_invalid_token_returns_401(client):
    resp = client.get("/auth/status", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["is_authenticated"] is False
    assert body["message"] == "未提供认证信息或登录已失效"


def test_non_bearer_scheme_returns_401(client):
    resp = client.get("/auth/status", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_unknown_user_returns_401(client):
    token = create_token("u_ghost", "u_ghost")
    resp = client.get("/auth/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["is_authenticated"] is False


def test_authenticated_returns_200(client):
    _make_user(client, "u_ok")
    token = create_token("u_ok", "u_ok")
    resp = client.get("/auth/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_authenticated"] is True
    assert body["user"]["user_code"] == "u_ok"


def test_disabled_user_returns_401(client):
    """W19：禁用账号在 status 自查同样视为未登录（_auth_user_dict 过滤）"""
    _make_user(client, "u_dis", is_active=False)
    token = create_token("u_dis", "u_dis")
    resp = client.get("/auth/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["is_authenticated"] is False
