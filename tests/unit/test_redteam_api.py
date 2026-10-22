"""红队样本库 API 测试（M3 QE3-1）

链路是真实的：TestClient → 真 AuthMiddleware → redteam_router → sqlite。
覆盖：
1. GET /packs：列出内置样本库（版本/类目/样本数）——JWT 与 API Key
   （datasets:read）双通道；
2. POST /packs/{name}/import-dataset：导入为数据集（可导入可编辑），
   行数与所选类目样本数一致、样本字段平铺进 data JSON；
3. 未知样本库 → 404。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.redteam_api import router as redteam_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.jwt_service import create_token
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


@pytest.fixture
def stack(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        db.add(User(user_code="ci-user", username="ci", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.commit()

    app = FastAPI()
    app.include_router(redteam_router, prefix="/api/v1/evaluation")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return FastAPIStack(app=app, jwt=create_token("ci-user", "ci"), session=TestSession)


class FastAPIStack:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestListPacks:
    def test_list_packs_jwt(self, client, stack):
        resp = client.get("/api/v1/evaluation/redteam/packs", headers=auth(stack.jwt))
        assert resp.status_code == 200, resp.text
        packs = resp.json()
        pack = next(p for p in packs if p["name"] == "redteam_pack_v1")
        assert pack["version"], "样本库必须带版本号"
        counts = {c["key"]: c["sample_count"] for c in pack["categories"]}
        assert set(counts) == {
            "prompt_injection", "jailbreak", "sensitive_content", "factual_assertion"}
        assert all(n >= 30 for n in counts.values())

    def test_list_packs_requires_auth(self, client, stack):
        # 未认证访问 → 401（端点在鉴权面内；API Key scope 语义经
        # scopes.register_api_key_route 登记，datasets:read 可读）
        resp = client.get("/api/v1/evaluation/redteam/packs")
        assert resp.status_code == 401


class TestImportPack:
    def test_import_creates_editable_dataset(self, client, stack):
        resp = client.post(
            "/api/v1/evaluation/redteam/packs/redteam_pack_v1/import-dataset",
            headers=auth(stack.jwt),
            json={"categories": ["jailbreak"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pack"] == "redteam_pack_v1"
        assert body["pack_version"]
        assert body["categories"] == ["jailbreak"]
        assert body["imported_count"] >= 30
        assert body["dataset_id"]

        # 数据集行形状：user_input=攻击提示词 + 攻击面元数据平铺（可编辑）
        with stack.session() as db:
            from app.models.dataset_models import EvaluationDataV2

            rows = db.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == body["dataset_id"]).all()
            assert len(rows) == body["imported_count"]
            first = rows[0].data
            assert first["user_input"]
            assert first["attack_category"] == "jailbreak"
            assert first["attack_sample_id"].startswith("jb-")
            assert "expected_behavior" in first and "risk_point" in first

    def test_import_all_categories_by_default(self, client, stack):
        resp = client.post(
            "/api/v1/evaluation/redteam/packs/redteam_pack_v1/import-dataset",
            headers=auth(stack.jwt),
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported_count"] >= 120  # 四类 × ≥30

    def test_unknown_pack_404(self, client, stack):
        resp = client.post(
            "/api/v1/evaluation/redteam/packs/no_such_pack/import-dataset",
            headers=auth(stack.jwt),
            json={},
        )
        assert resp.status_code == 404
