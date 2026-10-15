"""深健康检查集成测试（W13）

真实 PG（conftest 临时库，含 alembic 迁移）+ 真实 Redis（settings.REDIS_URL
同源，local_redis）+ 真实文件系统，验证全绿路径与端点 200。

degraded / 探针分支按 §3 W13 测试约定用 monkeypatch 覆盖，
见 tests/unit/test_health_deep.py（人为停 PG 场景本地难做）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_engine(runtime_db):
    """指向临时库的独立引擎（不碰全局 app engine，避免与供给逻辑耦合）"""
    engine = create_engine(runtime_db)
    yield engine
    engine.dispose()


def test_deep_health_all_green(runtime_engine, local_redis, monkeypatch, tmp_path):
    """四探针全绿：临时库已迁移到 head、Redis ping 通、uploads 可写"""
    from app.api import health_api
    from app.services import redis_stop_signal

    monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: local_redis)
    monkeypatch.setattr(health_api, "uploads_root", lambda: str(tmp_path / "uploads"))

    checks = health_api.collect_checks(engine=runtime_engine)

    assert checks["database"]["status"] == "ok", checks["database"]
    assert checks["redis"]["status"] == "ok", checks["redis"]
    assert checks["migrations"]["status"] == "ok", checks["migrations"]
    assert checks["uploads"]["status"] == "ok", checks["uploads"]
    # 迁移探针应给出具体版本
    assert checks["migrations"]["current"] == checks["migrations"]["expected"]
    assert checks["migrations"]["current"]  # 非空版本列表


def test_deep_health_endpoint_200(runtime_engine, local_redis, monkeypatch, tmp_path):
    """端到端：真实依赖全绿 → GET /health/deep 200 healthy"""
    from app.api import health_api
    from app.services import redis_stop_signal

    monkeypatch.setattr(health_api, "_app_engine", lambda: runtime_engine)
    monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: local_redis)
    monkeypatch.setattr(health_api, "uploads_root", lambda: str(tmp_path / "uploads"))

    app = FastAPI()
    app.include_router(health_api.router)
    client = TestClient(app)

    resp = client.get("/health/deep")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert set(body["checks"].keys()) == {"database", "redis", "migrations", "uploads"}
