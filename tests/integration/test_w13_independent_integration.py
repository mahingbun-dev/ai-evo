"""W13 独立测试角色补充集成验证（真实链路，不用 monkeypatch 探针函数体）

开发随附的 tests/integration/test_health_deep_integration.py 只覆盖全绿路径；
degraded 分支按 §3 W13 测试约定用 monkeypatch 覆盖（test_health_deep.py）。
本文件在本地可复现的范围内把两条 degraded 链路做成「真实探针」版本：

  1. 「人为停 PG」的本地等价：engine 指向本地确定无监听的端口（127.0.0.1:1，
     TCP 立即 ECONNREFUSED，等价于 PG 进程停止）——真实 probe_database /
     probe_migrations 连接失败 → 503 degraded，checks 给出明确 error。
     （不复刻 docker stop 的完整形态，但探针代码路径与停机完全一致：同一
     SQLAlchemy connect 失败路径。）
  2. 真实迁移版本漂移：临时库（w 前缀自建自清）迁移到 head 后，改写
     alembic_version 伪造「代码升级了但忘跑迁移」→ 真实 probe_migrations
     返回 mismatch 且 current/expected 分列；stamp 回 head 恢复后 ok。

复用 tests/integration/conftest.py 既有 fixture（runtime_db/shared_dsn/local_redis），
不新建 conftest。共享 dev 库零写入——全部写操作只落在临时库。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_engine(runtime_db):
    """指向临时库的独立引擎（复用 conftest 会话级临时库，已迁移到 head）"""
    engine = create_engine(runtime_db)
    yield engine
    engine.dispose()


@pytest.fixture
def dead_engine(shared_dsn):
    """「PG 已停机」的本地等价：共享库凭据 + 指向本地无监听端口（立即拒绝）"""
    from sqlalchemy.engine import make_url

    url = make_url(shared_dsn).set(host="127.0.0.1", port=1)
    engine = create_engine(url, connect_args={"connect_timeout": 2})
    yield engine
    engine.dispose()


class TestPgDownRealDegraded:
    def test_probes_real_connect_failure(self, dead_engine, local_redis, monkeypatch, tmp_path):
        """真实探针（不打桩）对不可达 engine：database/migrations down 且 error 明确"""
        from app.api import health_api
        from app.services import redis_stop_signal

        monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: local_redis)
        monkeypatch.setattr(health_api, "uploads_root", lambda: str(tmp_path / "uploads"))

        checks = health_api.collect_checks(engine=dead_engine)

        assert checks["database"]["status"] == "down", checks["database"]
        assert checks["database"]["error"], checks["database"]
        assert checks["migrations"]["status"] == "down", checks["migrations"]
        assert checks["migrations"]["error"], checks["migrations"]
        # 无关探针不受牵连
        assert checks["redis"]["status"] == "ok", checks["redis"]
        assert checks["uploads"]["status"] == "ok", checks["uploads"]

    def test_endpoint_returns_503_degraded(self, dead_engine, local_redis, monkeypatch, tmp_path):
        """端点级：真实依赖不可达 → 503 degraded（部署冒烟可发现的形态）"""
        from app.api import health_api
        from app.services import redis_stop_signal

        monkeypatch.setattr(health_api, "_app_engine", lambda: dead_engine)
        monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: local_redis)
        monkeypatch.setattr(health_api, "uploads_root", lambda: str(tmp_path / "uploads"))

        app = FastAPI()
        app.include_router(health_api.router)
        client = TestClient(app)

        resp = client.get("/health/deep")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"]["status"] == "down"
        assert body["checks"]["database"]["error"]


class TestMigrationDriftReal:
    def test_drift_detected_then_recovered(self, runtime_engine, runtime_db):
        """伪造 alembic_version 落后于 heads → 真实探针 mismatch（current/expected 分列）；
        写回真实 heads 恢复后 ok。全程只写临时库。"""
        from app.api import health_api

        # 前置：临时库已在 head
        assert health_api.probe_migrations(runtime_engine)["status"] == "ok"
        heads = health_api.migration_heads()

        # 伪造版本漂移（代码升级了但没跑迁移的库侧形态）
        with runtime_engine.begin() as conn:
            conn.exec_driver_sql("UPDATE alembic_version SET version_num = 'w13_drift_rev'")

        res = health_api.probe_migrations(runtime_engine)
        assert res["status"] == "mismatch", res
        assert res["current"] == ["w13_drift_rev"]
        assert res["expected"] == heads
        assert "alembic upgrade head" in res["error"]

        # 恢复：直接 SQL 写回真实 heads（不能用 alembic stamp——它会先解析表中
        # 的伪造版本号而拒绝执行；DELETE+INSERT 不留脏状态给后续用例）
        from sqlalchemy import text

        with runtime_engine.begin() as conn:
            conn.execute(text("DELETE FROM alembic_version"))
            for h in heads:
                conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": h})
        assert health_api.probe_migrations(runtime_engine)["status"] == "ok"
