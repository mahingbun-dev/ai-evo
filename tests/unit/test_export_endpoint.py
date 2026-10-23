"""导出端点 POST /evaluation/results/{task_id}/history/{execution_number}/export 测试。

两层覆盖：
1. 直调（w9_direct_call 惯例）：成功/幂等/400/404/401/502/500/403 语义分支，
   mock db 查询链 + 替换渲染与上传桩；
2. TestClient 门禁（W19b 教训：归属校验必须走真实链路）：独立 FastAPI app 挂载
   生产 results_router + sqlite 临时库 + get_db 依赖覆盖 + 注入 request.state.user
   的测试中间件（对齐 AuthMiddleware 的下游契约），真实 ensure_task_access 查库
   判 403/404；至少覆盖 401 缺凭证与 200 happy path 两例。

注意：AuthMiddleware 本体走 SSO+真实库 upsert（integration 级开销），本文件
不引入它；401 缺凭证断的是端点自身的 PLATFORM_SSO 校验，与中间件无关。
"""
import os
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.evaluation_task_api import ExecutionExportRequest, export_result_history_version
from app.services import hub_file_service, result_report
from app.services.hub_file_service import HubEndpointNotConfiguredError, HubFileError
from tests.unit.w9_direct_call import fake_request, fake_request_with_cookie

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

TEAM_A, TEAM_B = "EXP_A", "EXP_B"


# ---------------------------------------------------------------------------
# 公共桩
# ---------------------------------------------------------------------------


def _record(**kwargs):
    base = dict(
        id="exec_rec_1",
        execution_number=2,
        status="completed",
        result={"items": [{"idx": 0, "success": True}], "total": 1},
        total_cost=0.8,
        error_message=None,
        started_at=None,
        completed_at=None,
        config_snapshot={"model": "gpt"},
        exported_files=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _task(**kwargs):
    base = dict(
        id="task_x", name="导出冒烟任务", evaluation_type="judge",
        status="completed", result=None, total_cost=1.5,
        created_at=None, completed_at=None,
        execution_count=1, team_code=TEAM_A,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _req(cookie="PLATFORM_SSO=tok-test"):
    """直调用 Request 替身：带 client 属性（审计读取 request.client.host）。"""
    from tests.unit.w9_direct_call import SUPER_ADMIN_USER

    return SimpleNamespace(
        headers={"cookie": cookie},
        state=SimpleNamespace(user=SUPER_ADMIN_USER),
        client=None,
    )


def _db_with_task(task):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = task
    return db


def _record_service(by_number):
    svc = MagicMock()
    svc.get_record_by_number.return_value = by_number
    return svc


def _fake_hub_service(result=None, exc=None):
    """替换 HubFileService 类：捕获 upload_bytes 调用，可注入返回/异常。"""
    calls = []

    class _FakeHubFileService:
        def __init__(self, *args, **kwargs):
            pass

        def upload_bytes(self, filename, data):
            calls.append((filename, data))
            if exc is not None:
                raise exc
            payload = result or {
                "file_id": "file-1", "filename": filename,
                "size": len(data), "sha256": "deadbeef",
            }
            return dict(payload)

    return _FakeHubFileService, calls


def _run_export(monkeypatch, *, task, record, fmt="markdown", request=None,
                hub_result=None, hub_exc=None, svc=None, db=None, execution_number=None,
                markdown_builder=None):
    """直调端点的固定装置：打桩渲染/上传/审计，返回 (响应, upload_calls, logs)。"""
    from app.services import audit_service
    from app.services import evaluation_task_service as ets
    TaskExecutionRecordService_module = ets

    fake_cls, calls = _fake_hub_service(result=hub_result, exc=hub_exc)
    monkeypatch.setattr(hub_file_service, "HubFileService", fake_cls)
    # Cookie 通道名经配置注入（与 _req 的 PLATFORM_SSO 桩一致；默认空=通道禁用）
    from types import SimpleNamespace
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(HUB_SSO_COOKIE_NAME="PLATFORM_SSO"))
    monkeypatch.setattr(
        result_report, "build_markdown_export",
        markdown_builder or (lambda rec, t: "# markdown 报告全文".encode("utf-8")))
    monkeypatch.setattr(result_report, "build_json_export", lambda rec, t: b'{"lossless": true}')
    monkeypatch.setattr(result_report, "export_filename", lambda *a, **kw: "fake_report.md")
    logs = []
    monkeypatch.setattr(
        audit_service, "log_operation",
        lambda user, action, resource_type, resource_id, resource_name="", detail=None, ip_address="":
        logs.append({"action": action, "resource_type": resource_type,
                     "resource_id": resource_id, "detail": detail}),
    )
    svc = svc or _record_service(record)
    # 模块属性替换（既有模式）：函数内 import 在调用时读取，勿动真实类（__new__ 补丁会污染类）
    monkeypatch.setattr(TaskExecutionRecordService_module, "TaskExecutionRecordService", lambda db_arg: svc)

    if execution_number is None:
        execution_number = record.execution_number if record else 9
    req_obj = request or _req()
    resp = export_result_history_version(
        "task_x", execution_number,
        ExecutionExportRequest(format=fmt),
        req_obj,
        db=db or _db_with_task(task),
    )
    return resp, calls, logs, req_obj


# ---------------------------------------------------------------------------
# 直调：成功与幂等
# ---------------------------------------------------------------------------


class TestExportDirectSuccess:
    def test_markdown_export_success(self, monkeypatch):
        record = _record()
        resp, calls, logs, req_obj = _run_export(monkeypatch, task=_task(), record=record, fmt="markdown")

        assert resp == {
            "task_id": "task_x", "execution_number": 2, "format": "markdown",
            "cached": False, "file_id": "file-1", "filename": "fake_report.md",
            "size": len("# markdown 报告全文".encode("utf-8")), "sha256": "deadbeef",
        }
        # 渲染产物整体交给上传；以调用者凭证构造服务
        assert calls[0][1] == "# markdown 报告全文".encode("utf-8")
        assert calls[0][0] == "fake_report.md"
        # 上传成功后登记 + 导出审计改走路由级 audited 依赖（QE4-4）：
        # 直调断言 handler 暂存了审计标识（id/名称/format），真实落库由
        # test_audit_dependency/test_audit_coverage 把关；散点手工审计应不存在
        assert record.exported_files["markdown"]["file_id"] == "file-1"
        assert "uploaded_at" in record.exported_files["markdown"]
        stash = getattr(req_obj.state, "audit_resource", None)
        assert stash and stash["id"] == "exec_rec_1"
        assert stash["detail"]["format"] == "markdown"
        assert logs == []  # 不再散点手工 log_operation（路由依赖统一落库）

    def test_json_export_success(self, monkeypatch):
        resp, calls, _unused, _req_json = _run_export(monkeypatch, task=_task(), record=_record(), fmt="json")
        assert resp["format"] == "json"
        assert resp["cached"] is False
        assert calls[0][1] == b'{"lossless": true}'

    def test_cached_returns_file_id_without_upload_or_commit(self, monkeypatch):
        """exported_files 已登记：cached=true 直接复用，不再渲染/上传/写库。"""
        db = _db_with_task(_task())
        record = _record(exported_files={
            "markdown": {"file_id": "file-cached", "filename": "old.md", "size": 9, "sha256": "aa"},
        })
        resp, calls, logs, _req_cached = _run_export(monkeypatch, task=_task(), record=record, db=db)
        assert resp == {
            "task_id": "task_x", "execution_number": 2, "format": "markdown",
            "cached": True, "file_id": "file-cached", "filename": "old.md",
            "size": 9, "sha256": "aa",
        }
        assert calls == []  # upload 调用计数 0
        db.commit.assert_not_called()
        assert logs == []

    def test_cached_other_format_still_uploads(self, monkeypatch):
        """同版本不同格式互不影响：markdown 已登记，json 仍走上传。"""
        record = _record(exported_files={
            "markdown": {"file_id": "file-md", "filename": "old.md", "size": 9, "sha256": "aa"},
        })
        resp, calls, _, _ = _run_export(monkeypatch, task=_task(), record=record, fmt="json")
        assert resp["cached"] is False
        assert len(calls) == 1

    def test_non_db_exported_files_type_ignored(self, monkeypatch):
        """exported_files 非 dict（脏数据/历史行）：不上抛，走正常上传。"""
        record = _record(exported_files="not-a-dict")
        resp, calls, _, _ = _run_export(monkeypatch, task=_task(), record=record)
        assert resp["cached"] is False
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 直调：错误分支
# ---------------------------------------------------------------------------


class TestExportDirectErrors:
    def test_invalid_format_400(self, monkeypatch):
        with pytest.raises(HTTPException) as excinfo:
            _run_export(monkeypatch, task=_task(), record=_record(), fmt="pdf")
        assert excinfo.value.status_code == 400
        assert "pdf" in excinfo.value.detail

    def test_404_when_no_record_and_no_task_result(self, monkeypatch):
        with pytest.raises(HTTPException) as excinfo:
            _run_export(monkeypatch, task=_task(result=None), record=None)
        assert excinfo.value.status_code == 404

    def test_404_version_mismatch_on_compat_fallback(self, monkeypatch):
        """无记录 + 任务有结果：execution_number 与 execution_count 不符 → 404。"""
        with pytest.raises(HTTPException) as excinfo:
            _run_export(
                monkeypatch,
                task=_task(result={"total": 1}, execution_count=3),
                record=None,
                svc=_record_service(None),
                execution_number=2,
            )
        assert excinfo.value.status_code == 404

    def test_compat_fallback_success(self, monkeypatch):
        """旧任务无执行记录：按 execution_count 从任务本体构造兼容记录导出。"""
        captured = {}

        def fake_render(rec, t):
            captured["record"] = rec
            captured["task"] = t
            return b"# compat"

        task = _task(result={"total": 1}, execution_count=3, created_at=None, completed_at=None)
        resp, calls, logs, _req_compat = _run_export(
            monkeypatch, task=task, record=None, svc=_record_service(None), execution_number=3,
            markdown_builder=fake_render,
            hub_result={"file_id": "file-compat", "filename": "fake_report.md",
                        "size": 8, "sha256": "bb"},
        )
        assert resp["cached"] is False
        assert resp["file_id"] == "file-compat"
        rec = captured["record"]
        assert rec.execution_number == 3
        assert rec.result == {"total": 1}
        assert rec.config_snapshot is None
        assert rec.error_message is None
        # 非 DB 记录不写登记，审计改走路由级 audited 依赖（QE4-4）：
        # 直调断言 handler 暂存审计标识落在任务 id 上（is_db_record=False 分支）
        assert not hasattr(captured["record"], "exported_files") or captured["record"].exported_files is None
        stash = getattr(_req_compat.state, "audit_resource", None)
        assert stash and stash["id"] == "task_x"
        assert logs == []

    def test_missing_cookie_401_before_upload(self, monkeypatch):
        """无 PLATFORM_SSO cookie：401，且不发生上传。"""
        with pytest.raises(HTTPException) as excinfo:
            _run_export(
                monkeypatch, task=_task(), record=_record(),
                request=fake_request(),  # headers={} → 无凭证
            )
        assert excinfo.value.status_code == 401
        assert "HUB_SSO_COOKIE_NAME" in excinfo.value.detail

    def test_cookie_without_mas_token_401(self, monkeypatch):
        """有 cookie 但不含 PLATFORM_SSO：同样 401。"""
        with pytest.raises(HTTPException) as excinfo:
            _run_export(
                monkeypatch, task=_task(), record=_record(),
                request=fake_request_with_cookie(cookie="sso_session=abc; locale=zh-CN"),
            )
        assert excinfo.value.status_code == 401

    def test_hub_file_error_502(self, monkeypatch):
        with pytest.raises(HTTPException) as excinfo:
            _run_export(
                monkeypatch, task=_task(), record=_record(),
                hub_exc=HubFileError("file-store 上传失败：分块校验不符"),
            )
        assert excinfo.value.status_code == 502
        assert "上传失败" in excinfo.value.detail

    def test_endpoint_not_configured_500(self, monkeypatch):
        with pytest.raises(HTTPException) as excinfo:
            _run_export(
                monkeypatch, task=_task(), record=_record(),
                hub_exc=HubEndpointNotConfiguredError("ANC_HUB_MCP_ENDPOINT 未配置"),
            )
        assert excinfo.value.status_code == 500

    def test_access_denied_403_passthrough(self, monkeypatch):
        """归属校验 403（直调用 patch ensure_task_access 模拟，真实链路见 TestClient 门禁）。"""
        from app.api import evaluation_task_api

        monkeypatch.setattr(
            evaluation_task_api, "ensure_task_access",
            MagicMock(side_effect=HTTPException(status_code=403, detail="无权访问该资源")),
        )
        with pytest.raises(HTTPException) as excinfo:
            export_result_history_version(
                "task_x", 2, ExecutionExportRequest(format="markdown"),
                fake_request_with_cookie(), db=MagicMock(),
            )
        assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# TestClient 门禁（真实 ensure_task_access 链路 + sqlite）
# ---------------------------------------------------------------------------

_DB_DIR = tempfile.mkdtemp(prefix="export_gate_test_")
_engine = None
_TestingSessionLocal = None
CURRENT_USER = {"value": None}


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """把预设用户注入 request.state.user（对齐 AuthMiddleware 的下游契约）。"""

    async def dispatch(self, request, call_next):
        request.state.user = CURRENT_USER["value"]
        return await call_next(request)


def _build_gate_app():
    global _engine, _TestingSessionLocal
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.api.evaluation_task_api import results_router
    from app.db.session import Base, get_db
    import app.models.task_models as tm  # noqa: F401 注册表模型

    _engine = create_engine(
        f"sqlite:///{os.path.join(_DB_DIR, 'gate.sqlite')}",
        connect_args={"check_same_thread": False},
    )
    _TestingSessionLocal = sessionmaker(bind=_engine, autoflush=False)
    Base.metadata.create_all(
        bind=_engine,
        tables=[tm.EvaluationTask.__table__, tm.TaskExecutionRecord.__table__],
    )

    def _override_get_db():
        db = _TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(results_router, prefix="/api/v1")
    app.add_middleware(_FakeAuthMiddleware)
    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


_gate_client = _build_gate_app()


def _seed(task_id=None, team=TEAM_A, execution_number=1, exported_files=None):
    from app.models.task_models import EvaluationTask, TaskExecutionRecord

    task_id = task_id or f"task_gate_{uuid.uuid4().hex[:8]}"
    db = _TestingSessionLocal()
    try:
        db.add(EvaluationTask(
            id=task_id, name="门禁导出任务", evaluation_type="judge", status="completed",
            team_code=team, is_active=True, version=1,
            result={"items": [{"idx": 0}], "total": 1}, total_cost=0.5,
        ))
        db.add(TaskExecutionRecord(
            id=f"exec_gate_{task_id}_{execution_number}", task_id=task_id,
            execution_number=execution_number, status="completed",
            result={"items": [{"idx": 0, "success": True}], "total": 1},
            total_cost=0.5, error_message=None, config_snapshot=None,
            exported_files=exported_files,
        ))
        db.commit()
    finally:
        db.close()
    return task_id


def _cleanup(task_id):
    from app.models.task_models import EvaluationTask, TaskExecutionRecord

    db = _TestingSessionLocal()
    try:
        db.query(TaskExecutionRecord).filter(TaskExecutionRecord.task_id == task_id).delete()
        db.query(EvaluationTask).filter(EvaluationTask.id == task_id).delete()
        db.commit()
    finally:
        db.close()


def _as_user(org, super_admin_flag=0):
    CURRENT_USER["value"] = {
        "user_code": f"gate-user-{org}", "user_name": "gate", "org_code": org,
        "super_admin_flag": super_admin_flag,
    }


@pytest.fixture(autouse=True)
def _gate_audit_stub(monkeypatch):
    """TestClient 链路里把上传与审计打桩（鉴权/归属校验保持真实）。"""
    from app.services import audit_service

    fake_cls, _calls = _fake_hub_service(result={
        "file_id": "file-tc-1", "filename": "报告.md", "size": 10, "sha256": "cc",
    })
    monkeypatch.setattr(hub_file_service, "HubFileService", fake_cls)
    monkeypatch.setattr(
        audit_service, "log_operation", lambda *a, **kw: None,
    )


class TestExportGateViaTestClient:
    BASE = "/api/v1/evaluation/results"

    def test_401_without_mas_cookie(self, monkeypatch):
        """门禁一：有登录身份但请求无 PLATFORM_SSO 凭证 → 端点 401（不触发上传）。"""
        task_id = _seed()
        try:
            _as_user(TEAM_A)
            resp = _gate_client.post(f"{self.BASE}/{task_id}/history/1/export", json={"format": "markdown"})
            assert resp.status_code == 401, resp.text
            assert "HUB_SSO_COOKIE_NAME" in resp.json()["detail"]
        finally:
            _cleanup(task_id)

    def test_200_happy_path_persists_exported_files(self, monkeypatch):
        """门禁二：本团队用户 + 平台凭证 → 200，exported_files 落库。"""
        from types import SimpleNamespace

        from app.models.task_models import TaskExecutionRecord

        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(HUB_SSO_COOKIE_NAME="PLATFORM_SSO"))
        task_id = _seed()
        try:
            _as_user(TEAM_A)
            resp = _gate_client.post(
                f"{self.BASE}/{task_id}/history/1/export",
                json={"format": "markdown"},
                headers={"cookie": "PLATFORM_SSO=tok-gate-1; locale=zh-CN"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["task_id"] == task_id
            assert body["cached"] is False
            assert body["file_id"] == "file-tc-1"
            assert body["format"] == "markdown"
            assert body["size"] == 10

            db = _TestingSessionLocal()
            try:
                rec = db.query(TaskExecutionRecord).filter(
                    TaskExecutionRecord.task_id == task_id
                ).first()
                assert rec.exported_files["markdown"]["file_id"] == "file-tc-1"
                assert "uploaded_at" in rec.exported_files["markdown"]
            finally:
                db.close()
        finally:
            _cleanup(task_id)

    def test_403_cross_team(self):
        """门禁三：跨团队任务 → ensure_task_access 真实 403。"""
        task_id = _seed(team=TEAM_A)
        try:
            _as_user(TEAM_B)
            resp = _gate_client.post(
                f"{self.BASE}/{task_id}/history/1/export",
                json={"format": "markdown"},
                headers={"cookie": "PLATFORM_SSO=tok-gate-2"},
            )
            assert resp.status_code == 403, resp.text
            assert "跨团队" in resp.json()["detail"]
        finally:
            _cleanup(task_id)

    def test_403_bypassed_by_super_admin(self):
        """门禁四：超管跨团队放行（走到 401 之前的归属校验即通过）。"""
        task_id = _seed(team=TEAM_A)
        try:
            _as_user(TEAM_B, super_admin_flag=1)
            resp = _gate_client.post(
                f"{self.BASE}/{task_id}/history/1/export", json={"format": "markdown"}
            )
            # 超管通过归属校验；本用例未带凭证 → 卡在端点 401，证明归属放行
            assert resp.status_code == 401, resp.text
        finally:
            _cleanup(task_id)

    def test_404_task_missing(self):
        """门禁五：任务不存在 → 404（不泄漏归属信息）。"""
        _as_user(TEAM_A)
        resp = _gate_client.post(
            f"{self.BASE}/task_gate_missing/history/1/export", json={"format": "markdown"}
        )
        assert resp.status_code == 404, resp.text

    def test_idempotent_cached_via_test_client(self):
        """门禁六：已登记的导出走幂等（cached=true，不重复上传）。"""
        task_id = _seed(exported_files={
            "json": {"file_id": "file-tc-old", "filename": "old.json", "size": 5, "sha256": "dd"},
        })
        try:
            _as_user(TEAM_A)
            resp = _gate_client.post(
                f"{self.BASE}/{task_id}/history/1/export",
                json={"format": "json"},
                headers={"cookie": "PLATFORM_SSO=tok-gate-3"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["cached"] is True
            assert body["file_id"] == "file-tc-old"
        finally:
            _cleanup(task_id)
