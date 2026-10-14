"""深健康检查单元测试（W13）

按 §3 W13 测试约定：人为停 PG 的场景本地难做，用 monkeypatch 断言
degraded 输出结构与各探针分支（DB/Redis/迁移比对/uploads/超时）。
真实 PG+Redis 的全绿路径见 tests/integration/test_health_deep_integration.py。
"""
import time

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health_api
from app.middleware.auth_middleware import WHITELIST_PREFIXES


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_api.router)
    return TestClient(app)


def _patch_all_ok(monkeypatch):
    monkeypatch.setattr(health_api, "probe_database", lambda engine=None: {"status": "ok"})
    monkeypatch.setattr(health_api, "probe_redis", lambda: {"status": "ok"})
    monkeypatch.setattr(
        health_api,
        "probe_migrations",
        lambda engine=None: {"status": "ok", "current": ["rev1"], "expected": ["rev1"]},
    )
    monkeypatch.setattr(health_api, "probe_uploads", lambda root=None: {"status": "ok", "path": "/x"})


# ======================================================================
# 端点级：healthy / degraded 输出结构
# ======================================================================

class TestEndpointShape:
    def test_healthy_200(self, client, monkeypatch):
        _patch_all_ok(monkeypatch)
        resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["timestamp"]
        assert set(body["checks"].keys()) == {"database", "redis", "migrations", "uploads"}
        assert all(c["status"] == "ok" for c in body["checks"].values())

    def test_db_down_503_degraded(self, client, monkeypatch):
        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            health_api,
            "probe_database",
            lambda engine=None: {"status": "down", "error": "connection refused"},
        )
        resp = client.get("/health/deep")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"]["status"] == "down"
        assert "connection refused" in body["checks"]["database"]["error"]
        # 其余探针不受牵连
        assert body["checks"]["redis"]["status"] == "ok"
        assert body["checks"]["uploads"]["status"] == "ok"

    def test_migration_mismatch_503_with_current_expected(self, client, monkeypatch):
        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            health_api,
            "probe_migrations",
            lambda engine=None: {
                "status": "mismatch",
                "current": ["old_rev"],
                "expected": ["new_rev"],
                "error": "alembic_version=['old_rev'] 与迁移 heads=['new_rev'] 不一致，请执行 alembic upgrade head",
            },
        )
        resp = client.get("/health/deep")
        assert resp.status_code == 503
        mig = resp.json()["checks"]["migrations"]
        assert mig["status"] == "mismatch"
        assert mig["current"] == ["old_rev"]
        assert mig["expected"] == ["new_rev"]
        assert "alembic upgrade head" in mig["error"]

    def test_timeout_probe_down(self, client, monkeypatch):
        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(health_api, "PROBE_TIMEOUT_SECONDS", 0.1)

        def slow_probe(engine=None):
            time.sleep(0.6)
            return {"status": "ok"}

        monkeypatch.setattr(health_api, "probe_database", slow_probe)
        resp = client.get("/health/deep")
        assert resp.status_code == 503
        db = resp.json()["checks"]["database"]
        assert db["status"] == "down"
        assert "超时" in db["error"]

    def test_probe_unexpected_exception_translated(self, client, monkeypatch):
        _patch_all_ok(monkeypatch)

        def broken(engine=None):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(health_api, "probe_database", broken)
        resp = client.get("/health/deep")
        assert resp.status_code == 503
        assert resp.json()["checks"]["database"]["status"] == "down"


def test_deep_health_in_auth_whitelist():
    """探活不带业务 cookie，必须放行（W13）"""
    assert "/api/v1/health" in WHITELIST_PREFIXES


# ======================================================================
# 探针级分支
# ======================================================================

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None, exc=None):
        self._rows = rows
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt):
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows=None, exc=None):
        self._rows = rows
        self._exc = exc

    def connect(self):
        return _FakeConn(self._rows, self._exc)


class TestProbeDatabase:
    def test_ok(self):
        res = health_api.probe_database(_FakeEngine())
        assert res["status"] == "ok"

    def test_connect_error_down(self):
        res = health_api.probe_database(_FakeEngine(exc=OSError("could not connect")))
        assert res["status"] == "down"
        assert "could not connect" in res["error"]

    def test_none_global_engine_down(self, monkeypatch):
        # probe_database(engine=None) 即「用全局引擎」的生产路径；
        # 单测里 monkeypatch 掉全局引擎获取函数，模拟引擎未初始化
        # （注：不能靠"引擎恰好是 None"断言——集成 conftest 注入 DATABASE_URL
        # 后全局引擎是真实引擎，同一用例会随测试组合翻转）
        monkeypatch.setattr(health_api, "_app_engine", lambda: None)
        res = health_api.probe_database()
        assert res["status"] == "down"
        assert "引擎未初始化" in res["error"]

    def test_none_global_engine_migrations_down(self, monkeypatch):
        monkeypatch.setattr(health_api, "_app_engine", lambda: None)
        monkeypatch.setattr(health_api, "migration_heads", lambda: ["rev1"])
        res = health_api.probe_migrations()
        assert res["status"] == "down"
        assert res["expected"] == ["rev1"]


class TestProbeRedis:
    def test_ok(self, monkeypatch):
        from app.services import redis_stop_signal

        class _FakeClient:
            def ping(self):
                return True

        monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: _FakeClient())
        assert health_api.probe_redis() == {"status": "ok"}

    def test_down(self, monkeypatch):
        from app.services import redis_stop_signal

        def boom():
            raise OSError("redis down")

        monkeypatch.setattr(redis_stop_signal, "get_redis_client", boom)
        res = health_api.probe_redis()
        assert res["status"] == "down"
        assert "redis down" in res["error"]


class TestProbeMigrations:
    def test_ok_when_current_matches_heads(self, monkeypatch):
        monkeypatch.setattr(health_api, "migration_heads", lambda: ["rev1"])
        res = health_api.probe_migrations(_FakeEngine(rows=[("rev1",)]))
        assert res["status"] == "ok"
        assert res["current"] == ["rev1"]
        assert res["expected"] == ["rev1"]

    def test_mismatch(self, monkeypatch):
        monkeypatch.setattr(health_api, "migration_heads", lambda: ["rev2"])
        res = health_api.probe_migrations(_FakeEngine(rows=[("rev1",)]))
        assert res["status"] == "mismatch"
        assert res["current"] == ["rev1"]
        assert res["expected"] == ["rev2"]

    def test_missing_alembic_version_table_down(self, monkeypatch):
        monkeypatch.setattr(health_api, "migration_heads", lambda: ["rev1"])
        res = health_api.probe_migrations(
            _FakeEngine(exc=Exception('relation "alembic_version" does not exist'))
        )
        assert res["status"] == "down"
        assert "alembic_version" in res["error"]


class TestProbeUploads:
    def test_ok_writes_and_cleans_temp_file(self, tmp_path):
        d = tmp_path / "uploads"
        res = health_api.probe_uploads(str(d))
        assert res["status"] == "ok"
        assert res["path"] == str(d)
        # 探测文件已清理
        assert list(d.iterdir()) == []

    def test_down_when_readonly(self, tmp_path):
        import os

        d = tmp_path / "uploads"
        d.mkdir()
        d.chmod(0o555)
        try:
            if os.access(d, os.W_OK):
                pytest.skip("当前用户绕过目录只读位（如 root），无法构造不可写场景")
            res = health_api.probe_uploads(str(d))
            assert res["status"] == "down"
            assert res["error"]
        finally:
            d.chmod(0o755)
