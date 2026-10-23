"""并发池 HTTP API 单测（假 Redis 客户端 / 假 DB / 假审计，无外部依赖）

路由级 ensure_super_admin 依赖在 import 时已绑定到 APIRouter 上，
因此权限放行只能用 app.dependency_overrides，不能在模块属性上 patch。
操作人归属通过中间件写入 request.state.user 验证（真实链路由
AuthMiddleware 写入同样的结构）。
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

from app.api import judge_pool_api as api  # noqa: E402
from app.services.judge_pool.config import DEFAULTS  # noqa: E402
from app.services.judge_pool.engine import Engine  # noqa: E402


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, release_result=True, engine_count=3, snapshot=None):
        self.release_result = release_result
        self.engine_count = engine_count
        self.released_calls = []
        self.released_engines = []
        self._snapshot = snapshot

    def snapshot(self):
        if self._snapshot is not None:
            return self._snapshot
        return {
            "redis_available": True,
            "degraded": False,
            "engines": {
                "graph": {"limit": 16, "active": 1, "available": 15, "waiting": 0, "peak_1m": 2},
                "claude_agent": {"limit": 8, "active": 0, "available": 8, "waiting": 0, "peak_1m": 0},
                "harness": {"limit": 8, "active": 0, "available": 8, "waiting": 0, "peak_1m": 0},
                "plugin": {"limit": 4, "active": 0, "available": 4, "waiting": 0, "peak_1m": 0},
            },
            "leases": [],
        }

    def force_release(self, engine, lease_id):
        self.released_calls.append((Engine(engine), lease_id))
        return self.release_result

    def force_release_engine(self, engine):
        self.released_engines.append(Engine(engine))
        return self.engine_count


class _FakeDb:
    """只支持 /status 里任务名补全那一次 query"""

    def __init__(self, rows=None):
        self.rows = rows or []

    def query(self, *columns):
        return self

    def filter(self, *conditions):
        return self

    def all(self):
        return self.rows


class _AuditRow:
    id = 7
    event = "manual_release"
    engine = "graph"
    lease_id = "lease-x"
    eval_task_id = "task-1"
    row_idx = 3
    wait_ms = 12
    hold_ms = 34
    host = "host-a"
    pid = 123
    detail = {"scope": "single", "operator": "u-1"}
    created_at = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def audit_events(monkeypatch):
    """拦下真实 record_event：单测绝不能拉起审计写线程去连 DB"""
    events = []
    monkeypatch.setattr(api, "record_event", lambda event, **kw: events.append((event, kw)))
    return events


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []

    def _query(db, engine=None, event=None, start=None, end=None, skip=0, limit=50):
        calls.append({"engine": engine, "event": event, "start": start, "end": end,
                      "skip": skip, "limit": limit})
        return [_AuditRow()], 1

    monkeypatch.setattr(api, "query_audit", _query)
    return calls


@pytest.fixture
def make_client(monkeypatch, audit_events, audit_calls):
    """按需构造 TestClient；user 为写入 request.state.user 的登录上下文"""

    def _make(user=None, db=None, config=None, client=None):
        monkeypatch.setattr(api, "load_config", lambda force=False: config or DEFAULTS)
        monkeypatch.setattr(api, "save_config",
                            lambda payload, updated_by="": config or DEFAULTS)
        fake = client or _FakeClient()
        monkeypatch.setattr(api, "get_pool_client", lambda: fake)
        app = FastAPI()

        @app.middleware("http")
        async def _inject_user(request, call_next):
            request.state.user = user if user is not None else {"user_code": "u-1", "super_admin_flag": 1}
            return await call_next(request)

        app.include_router(api.router)
        # 路由依赖在 import 时已绑定，必须走 dependency_overrides
        app.dependency_overrides[api.ensure_super_admin] = lambda: None
        app.dependency_overrides[api.get_db] = lambda: db if db is not None else _FakeDb()
        return TestClient(app)

    return _make


# ---------------------------------------------------------------------------
# 监控
# ---------------------------------------------------------------------------


def test_status_shape(make_client):
    body = make_client().get("/judge-pool/status").json()
    assert body["redis_available"] is True
    assert body["degraded"] is False
    assert set(body["engines"]) == {e.value for e in Engine}
    assert body["engines"]["graph"]["limit"] == 16
    assert body["leases"] == []


def test_status_enriches_task_name(make_client):
    snapshot = _FakeClient().snapshot()
    snapshot["leases"] = [{"lease_id": "l1", "eval_task_id": "task-1"}]
    client = make_client(db=_FakeDb(rows=[("task-1", "任务一")]),
                        client=_FakeClient(snapshot=snapshot))
    body = client.get("/judge-pool/status").json()
    assert body["leases"][0]["task_name"] == "任务一"


def test_status_redis_down_is_200_degraded(make_client):
    """裁决：Redis 故障返回 200 + degraded，前端才能渲染橙色降级横幅"""
    degraded = {"redis_available": False, "degraded": True,
                "error": "redis down", "engines": {}, "leases": []}
    resp = make_client(client=_FakeClient(snapshot=degraded)).get("/judge-pool/status")
    assert resp.status_code == 200
    assert resp.json()["degraded"] is True


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_get_config(make_client):
    body = make_client().get("/judge-pool/config").json()
    assert body["graph_limit"] == 16
    assert body["lease_ttl_seconds"] == 180


def test_put_config_heartbeat_not_less_than_half_ttl_400(make_client):
    resp = make_client().put(
        "/judge-pool/config",
        json={"heartbeat_interval_seconds": 200, "lease_ttl_seconds": 180},
    )
    assert resp.status_code == 400
    assert "heartbeat_interval_seconds" in resp.json()["detail"]


def test_put_config_heartbeat_against_current_ttl_400(make_client):
    """只改心跳时，要按当前 ttl 校验（默认 ttl=180 → 90 已越界）"""
    resp = make_client().put("/judge-pool/config", json={"heartbeat_interval_seconds": 100})
    assert resp.status_code == 400


def test_put_config_value_error_from_save_is_400(make_client, monkeypatch, audit_events):
    client = make_client()  # 先建 client（它会装上默认替身），再覆盖 save_config

    def _boom(payload, updated_by=""):
        raise ValueError("fail_open 无法解析为布尔值")

    monkeypatch.setattr(api, "save_config", _boom)
    resp = client.put("/judge-pool/config", json={"graph_limit": 32})
    assert resp.status_code == 400
    assert "fail_open" in resp.json()["detail"]


def test_put_config_ok_passes_updated_by(make_client, monkeypatch, audit_events):
    seen = {}
    from app.services.judge_pool.config import JudgePoolConfig

    def _save(payload, updated_by=""):
        seen.update(payload=payload, updated_by=updated_by)
        return JudgePoolConfig(**{**DEFAULTS.to_dict(), **payload})

    client = make_client()
    monkeypatch.setattr(api, "save_config", _save)
    resp = client.put("/judge-pool/config", json={"graph_limit": 32})
    assert resp.status_code == 200
    assert resp.json()["graph_limit"] == 32
    assert seen["payload"] == {"graph_limit": 32}
    assert seen["updated_by"] == "u-1"


def test_put_config_operator_falls_back_to_username(make_client, monkeypatch):
    seen = {}
    client = make_client(user={"username": "alice", "super_admin_flag": 1})
    monkeypatch.setattr(api, "save_config",
                        lambda payload, updated_by="": seen.update(updated_by=updated_by) or DEFAULTS)
    resp = client.put("/judge-pool/config", json={"graph_limit": 32})
    assert resp.status_code == 200
    assert seen["updated_by"] == "alice"


# ---------------------------------------------------------------------------
# 强制释放
# ---------------------------------------------------------------------------


def test_force_release_success(make_client, audit_events):
    fake_client = _FakeClient()
    resp = make_client(client=fake_client).post("/judge-pool/leases/abc/release?engine=graph")
    assert resp.status_code == 200
    assert resp.json()["released"] is True
    assert fake_client.released_calls == [(Engine.GRAPH, "abc")]
    assert len(audit_events) == 1
    event, kw = audit_events[0]
    assert event == "manual_release"
    assert kw["engine"] == "graph"
    assert kw["detail"] == {"scope": "single", "operator": "u-1"}


def test_force_release_missing_lease_no_audit(make_client, audit_events):
    resp = make_client(client=_FakeClient(release_result=False)).post(
        "/judge-pool/leases/gone/release?engine=graph"
    )
    assert resp.status_code == 200
    assert resp.json()["released"] is False
    assert audit_events == []


def test_force_release_invalid_engine_400(make_client, audit_events):
    resp = make_client().post("/judge-pool/leases/abc/release?engine=bogus")
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]
    assert audit_events == []


def test_force_release_requires_engine(make_client):
    assert make_client().post("/judge-pool/leases/abc/release").status_code == 422


def test_release_all_with_engine(make_client, audit_events):
    fake_client = _FakeClient()
    resp = make_client(client=fake_client).post("/judge-pool/release-all?engine=graph")
    assert resp.json()["released"] == 3
    assert fake_client.released_engines == [Engine.GRAPH]
    assert len(audit_events) == 1
    event, kw = audit_events[0]
    assert event == "manual_release"
    assert kw["engine"] == "graph"
    assert kw["detail"] == {"scope": "all", "count": 3, "operator": "u-1"}


def test_release_all_without_engine_covers_all(make_client, audit_events):
    fake_client = _FakeClient()
    resp = make_client(client=fake_client).post("/judge-pool/release-all")
    assert resp.json()["released"] == 12  # 4 引擎 × 3
    assert fake_client.released_engines == list(Engine)
    assert [kw["engine"] for _, kw in audit_events] == [e.value for e in Engine]
    assert all(kw["detail"]["scope"] == "all" for _, kw in audit_events)


def test_release_all_nothing_released_no_audit(make_client, audit_events):
    resp = make_client(client=_FakeClient(engine_count=0)).post("/judge-pool/release-all")
    assert resp.json()["released"] == 0
    assert audit_events == []


def test_release_all_invalid_engine_400(make_client, audit_events):
    resp = make_client().post("/judge-pool/release-all?engine=bogus")
    assert resp.status_code == 400
    assert audit_events == []


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_audit_pagination_shape(make_client, audit_calls):
    resp = make_client().get("/judge-pool/audit?page=2&page_size=10&engine=graph&event=manual_release")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["lease_id"] == "lease-x"
    assert item["detail"] == {"scope": "single", "operator": "u-1"}
    assert item["created_at"] == "2026-10-23T08:00:00+00:00"
    assert audit_calls == [{"engine": "graph", "event": "manual_release", "start": None,
                            "end": None, "skip": 10, "limit": 10}]


def test_audit_page_size_bounds(make_client):
    assert make_client().get("/judge-pool/audit?page_size=500").status_code == 422
    assert make_client().get("/judge-pool/audit?page=0").status_code == 422
