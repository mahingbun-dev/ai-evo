"""QE4-4 audited 审计依赖运行时行为测试

机制（app/services/audit_service.py::audited）：yield 型依赖 teardown 在
handler 成功返回后落 operation_logs；端点抛 HTTPException 不落库（只记成功的写）；
资源标识解析：stash（request.state.audit_resource）→ id_param 路径参数 →
唯一路径参数；API Key 通道 detail 标注 via=api_key。
"""
import pytest
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.audit_models import OperationLog
from app.services.audit_service import audited, record_route_audit

pytestmark = pytest.mark.unit

ADMIN = {"user_code": "u_admin", "user_name": "管理员", "org_code": "QE",
         "super_admin_flag": 1, "role": "owner"}


class _UserInjectMiddleware:
    """把固定用户字典注到 request.state（生产由 AuthMiddleware 完成同构注入）"""

    def __init__(self, app, user, auth_type=None):
        self.app, self.user, self.auth_type = app, user, auth_type

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.requests import Request
            request = Request(scope)
            request.state.user = self.user
            if self.auth_type:
                request.state.auth_type = self.auth_type
        await self.app(scope, receive, send)


@pytest.fixture()
def audit_db(tmp_path, monkeypatch):
    """独立 sqlite 库承载 audit_service.SessionLocal（独立 session 语义同生产）"""
    engine = create_engine(
        f"sqlite:///{tmp_path/'audit.db'}",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[OperationLog.__table__])
    Testing = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    import app.db.session as session_mod
    import app.services.audit_service as audit_mod
    monkeypatch.setattr(session_mod, "SessionLocal", Testing)
    monkeypatch.setattr(audit_mod, "SessionLocal", Testing)
    yield Testing
    engine.dispose()


def _make_app(user=ADMIN, auth_type=None):
    app = FastAPI()
    app.add_middleware(_UserInjectMiddleware, user=user, auth_type=auth_type)

    @app.post("/things", dependencies=[Depends(audited("CREATE", "thing"))])
    def create_thing(request: Request):
        request.state.audit_resource = {"id": "th_1", "name": "新资源", "detail": {"k": "v"}}
        return {"id": "th_1"}

    @app.post("/things", name="create_no_stash",
             dependencies=[Depends(audited("CREATE", "thing"))])
    def unused():  # pragma: no cover - 占位不被调用
        return {}

    @app.put("/things/{thing_id}", dependencies=[Depends(audited("UPDATE", "thing", id_param="thing_id"))])
    def update_thing(thing_id: str):
        return {"ok": True}

    @app.delete("/things/{thing_id}", dependencies=[Depends(audited("DELETE", "thing", id_param="thing_id"))])
    def delete_thing(thing_id: str):
        raise HTTPException(status_code=404, detail="不存在")

    @app.post("/jobs/{job_id}", dependencies=[Depends(audited("RUN", "job"))])
    def run_job(job_id: str):
        return {"ok": True}

    return app


class TestAuditedDependency:
    def test_success_logs_with_stash(self, audit_db):
        client = TestClient(_make_app())
        resp = client.post("/things")
        assert resp.status_code == 200
        with audit_db() as db:
            logs = db.query(OperationLog).all()
        assert len(logs) == 1
        log = logs[0]
        assert log.action == "CREATE" and log.resource_type == "thing"
        assert log.resource_id == "th_1" and log.resource_name == "新资源"
        assert log.user_code == ADMIN["user_code"] and log.team_code == "QE"
        assert log.detail["k"] == "v" and log.detail["method"] == "POST"
        assert log.detail["path"].endswith("/things")

    def test_failure_does_not_log(self, audit_db):
        """端点抛 HTTPException（404）→ teardown 不执行，审计不落库"""
        client = TestClient(_make_app())
        assert client.delete("/things/th_1").status_code == 404
        with audit_db() as db:
            assert db.query(OperationLog).count() == 0

    def test_id_param_path_extraction(self, audit_db):
        client = TestClient(_make_app())
        assert client.put("/things/th_42").status_code == 200
        with audit_db() as db:
            log = db.query(OperationLog).one()
        assert log.resource_id == "th_42" and log.action == "UPDATE"
        assert log.resource_name == ""  # 未 stash 名称则为空

    def test_sole_path_param_fallback(self, audit_db):
        """多路径参数未指定 id_param 时取唯一参数；这里 job 路由只有 job_id"""
        client = TestClient(_make_app())
        assert client.post("/jobs/job_9").status_code == 200
        with audit_db() as db:
            log = db.query(OperationLog).one()
        assert log.resource_id == "job_9" and log.action == "RUN"

    def test_api_key_channel_marked(self, audit_db):
        """API Key 通道（auth_type=api_key）落审计 detail 标注 via=api_key"""
        client = TestClient(_make_app(auth_type="api_key"))
        assert client.put("/things/th_7").status_code == 200
        with audit_db() as db:
            log = db.query(OperationLog).one()
        assert log.detail.get("via") == "api_key"

    def test_action_uppercased_marker_present(self):
        """依赖函数带 __audit_action__/__audit_resource_type__ 静态标记（小写自动转大写）"""
        dep = audited("create", "thing", id_param="thing_id")
        assert dep.__audit_action__ == "CREATE"
        assert dep.__audit_resource_type__ == "thing"
        assert dep.__audit_id_param__ == "thing_id"

    def test_record_route_audit_never_raises(self, audit_db, monkeypatch):
        """审计落库失败（DB 抛错）不阻断业务响应——log_operation 内部吞掉"""
        import app.services.audit_service as audit_mod
        monkeypatch.setattr(audit_mod, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        from starlette.requests import Request

        def call():
            scope = {"type": "http", "method": "POST", "path": "/x",
                     "headers": [], "query_string": b""}
            request = Request(scope)
            request.state.user = ADMIN
            record_route_audit(request, "CREATE", "thing")

        call()  # 不抛即通过
