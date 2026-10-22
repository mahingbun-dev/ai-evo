"""M1 QE1-2 门禁语义测试：gate_service 全分支 + 收尾路径落库 + 查询端点。

三层覆盖：
1. 纯函数（evaluate_gate / build_error_gate_result）：all/any、维度指标、
   metric 解析与不可用、算子白名单、未启用/未配阈值；
2. 落库链路（SQLite 内存库 + TaskExecutionRecordService）：complete 带
   判定、fail/stop 收尾自动落 error verdict、未启用门禁不落、已有判定不覆盖；
3. 查询端点（TestClient + 生产 router + 注入登录态的中间件，W19b 口径：
   归属校验必须走真实链路）：/executions/{n} 与 /gate 轻端点语义矩阵。
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.evaluation_task_api import results_router, router as task_router
from app.core import scopes as scope_constants
from app.db.session import Base, get_db
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.services.evaluation_task_service import (
    EvaluationTaskService,
    TaskExecutionRecordService,
)
from app.services.gate_service import (
    VERDICT_ERROR,
    VERDICT_FAILED,
    VERDICT_PASSED,
    build_error_gate_result,
    evaluate_gate,
    gate_enabled,
)

pytestmark = pytest.mark.unit

AGG = {
    "total": 10,
    "success_count": 9,
    "success_rate": 0.9,
    "average_score": 8.5,
    "dimension_scores": {"准确性": {"average": 7.2, "count": 9, "weight": 1}},
}


# ---------------- 1. 纯函数：evaluate_gate ----------------

CFG = {
    "enabled": True,
    "mode": "all",
    "thresholds": [{"metric": "total_score", "op": ">=", "value": 8.0}],
}


def test_disabled_gate_returns_none():
    assert evaluate_gate(None, AGG) is None
    assert evaluate_gate({}, AGG) is None
    assert evaluate_gate({"enabled": False, "thresholds": []}, AGG) is None
    assert gate_enabled(None) is False
    assert gate_enabled({"thresholds": [{}]}) is True  # 有配置没写 enabled 按启用


def test_all_mode_pass_and_fail():
    cfg = {"enabled": True, "mode": "all", "thresholds": [
        {"metric": "total_score", "op": ">=", "value": 8.0},
        {"metric": "pass_rate", "op": ">=", "value": 0.95},
    ]}
    result = evaluate_gate(cfg, AGG)
    assert result["verdict"] == VERDICT_FAILED  # 8.5 过 / 0.9 不过
    assert result["mode"] == "all"
    checks = {c["metric"]: c for c in result["checks"]}
    assert checks["total_score"]["passed"] is True
    assert checks["total_score"]["actual"] == 8.5
    assert checks["pass_rate"]["passed"] is False
    assert checks["pass_rate"]["actual"] == 0.9
    assert result["evaluated_at"]

    ok = evaluate_gate(cfg, {**AGG, "success_rate": 0.97})
    assert ok["verdict"] == VERDICT_PASSED


def test_any_mode():
    cfg = {"enabled": True, "mode": "any", "thresholds": [
        {"metric": "total_score", "op": ">=", "value": 9.9},   # 不过
        {"metric": "pass_rate", "op": ">=", "value": 0.8},     # 过
    ]}
    assert evaluate_gate(cfg, AGG)["verdict"] == VERDICT_PASSED
    none_pass = {"enabled": True, "mode": "any", "thresholds": [
        {"metric": "total_score", "op": ">=", "value": 9.9},
    ]}
    assert evaluate_gate(none_pass, AGG)["verdict"] == VERDICT_FAILED


def test_dimension_metric():
    cfg = {"enabled": True, "thresholds": [
        {"metric": "dimension:准确性", "op": ">=", "value": 7.0}]}
    assert evaluate_gate(cfg, AGG)["verdict"] == VERDICT_PASSED
    strict = {"enabled": True, "thresholds": [
        {"metric": "dimension:准确性", "op": ">=", "value": 8.0}]}
    assert evaluate_gate(strict, AGG)["verdict"] == VERDICT_FAILED
    # 维度不存在 → metric 不可用 → error（配置引用了不存在的维度）
    missing = {"enabled": True, "thresholds": [
        {"metric": "dimension:不存在", "op": ">=", "value": 1.0}]}
    assert evaluate_gate(missing, AGG)["verdict"] == VERDICT_ERROR


def test_unknown_metric_yields_error_not_failed():
    """M2 的 verification_pass_rate 在 M2 接线前不可用 → error（退出码 2），
    与质量不达标（failed，退出码 1）严格区分"""
    cfg = {"enabled": True, "thresholds": [
        {"metric": "verification_pass_rate", "op": ">=", "value": 1.0}]}
    result = evaluate_gate(cfg, AGG)
    assert result["verdict"] == VERDICT_ERROR
    assert "不可用" in result["checks"][0]["note"]


def test_operators_and_bad_config():
    for op, value, expected in (("<=", 9.0, True), (">", 8.6, False),
                                ("<", 8.6, True), ("==", 8.5, True)):
        cfg = {"enabled": True, "thresholds": [
            {"metric": "total_score", "op": op, "value": value}]}
        assert evaluate_gate(cfg, AGG)["verdict"] == (
            VERDICT_PASSED if expected else VERDICT_FAILED), op
    # 未知算子 / 空阈值 / 非对象阈值项 → error
    assert evaluate_gate({"enabled": True, "thresholds": [
        {"metric": "total_score", "op": "~", "value": 1}]}, AGG)["verdict"] == VERDICT_ERROR
    assert evaluate_gate({"enabled": True, "thresholds": []}, AGG)["verdict"] == VERDICT_ERROR
    assert evaluate_gate({"enabled": True, "thresholds": ["junk"]}, AGG)["verdict"] == VERDICT_ERROR


def test_empty_aggregates_is_error():
    """空聚合（无任何行完成）拿不到指标 → error，不是 passed"""
    cfg = {"enabled": True, "thresholds": [
        {"metric": "total_score", "op": ">=", "value": 0}]}
    assert evaluate_gate(cfg, {})["verdict"] == VERDICT_ERROR


def test_build_error_gate_result():
    assert build_error_gate_result(None, "x") is None
    assert build_error_gate_result({"enabled": False}, "x") is None
    result = build_error_gate_result(CFG, "soft time limit")
    assert result["verdict"] == VERDICT_ERROR
    assert "soft time limit" in result["reason"]


# ---------------- 2. 落库链路（收尾路径全覆盖） ----------------


@pytest.fixture
def db_session():
    # TestClient 在独立线程跑 app：内存库须放开 same-thread 限制并用
    # StaticPool 保证跨线程共享同一连接（否则 app 线程看到的是空库）
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _seed_task(db, gate_config=None):
    db.add(EvaluationTask(
        id="task_gate", name="门禁任务", evaluation_type="judge", status="running",
        execution_count=1, team_code="DEFAULT", gate_config=gate_config))
    db.add(TaskExecutionRecord(id="exec_r1", task_id="task_gate",
                               execution_number=1, status="running"))
    db.commit()


def test_complete_record_persists_gate_result(db_session):
    _seed_task(db_session, CFG)
    service = TaskExecutionRecordService(db_session)
    verdict = evaluate_gate(CFG, AGG)  # CFG 单阈值 8.5>=8.0 → passed
    service.complete_record("task_gate", 1, AGG, gate_result=verdict)
    record = service.get_record_by_number("task_gate", 1)
    assert record.status == "completed"
    assert record.gate_result["verdict"] == VERDICT_PASSED
    assert record.gate_result["checks"][0]["metric"] == "total_score"


def test_complete_record_without_gate_keeps_null(db_session):
    _seed_task(db_session, None)  # 未启用门禁
    service = TaskExecutionRecordService(db_session)
    service.complete_record("task_gate", 1, AGG)
    assert service.get_record_by_number("task_gate", 1).gate_result is None


def test_fail_record_attaches_error_verdict(db_session):
    """失败收尾（软超时/on_failure 同走 fail_task → fail_record）落 error verdict"""
    _seed_task(db_session, CFG)
    service = TaskExecutionRecordService(db_session)
    service.fail_record("task_gate", 1, "soft time limit")
    record = service.get_record_by_number("task_gate", 1)
    assert record.status == "failed"
    assert record.gate_result["verdict"] == VERDICT_ERROR
    assert "soft time limit" in record.gate_result["reason"]


def test_stop_record_attaches_error_verdict(db_session):
    _seed_task(db_session, CFG)
    service = TaskExecutionRecordService(db_session)
    service.stop_record("task_gate", 1, partial_result=None)
    record = service.get_record_by_number("task_gate", 1)
    assert record.status == "stopped"
    assert record.gate_result["verdict"] == VERDICT_ERROR
    assert "停止" in record.gate_result["reason"]


def test_terminal_paths_no_gate_no_column(db_session):
    """未启用门禁的任务：fail/stop 收尾不落 gate_result（列保持 NULL）"""
    _seed_task(db_session, None)
    service = TaskExecutionRecordService(db_session)
    service.fail_record("task_gate", 1, "boom")
    assert service.get_record_by_number("task_gate", 1).gate_result is None


def test_fail_record_does_not_overwrite_existing_verdict(db_session):
    """已有判定（complete 落库后竞态到达的 fail）不覆盖"""
    _seed_task(db_session, CFG)
    service = TaskExecutionRecordService(db_session)
    service.complete_record("task_gate", 1, AGG, gate_result=evaluate_gate(CFG, AGG))
    service.fail_record("task_gate", 1, "late failure")  # 竞态路径
    record = service.get_record_by_number("task_gate", 1)
    assert record.gate_result["verdict"] == VERDICT_PASSED  # 保留原判定不被覆盖


def test_complete_task_forwards_gate_result(db_session):
    """complete_task（run_evaluation_async 的调用面）透传 gate_result 到执行记录"""
    _seed_task(db_session, CFG)
    service = EvaluationTaskService(db_session)
    service.complete_task("task_gate", AGG, gate_result=evaluate_gate(CFG, AGG))
    record = TaskExecutionRecordService(db_session).get_record_by_number("task_gate", 1)
    assert record.gate_result["verdict"] == VERDICT_PASSED


# ---------------- 3. 查询端点（真实路由 + 归属校验链路） ----------------


class _AuthStub(BaseHTTPMiddleware):
    """注入 request.state.user（对齐 AuthMiddleware 下游契约，W19b 口径）"""

    def __init__(self, app, user):
        super().__init__(app)
        self._user = user

    async def dispatch(self, request: Request, call_next):
        request.state.user = self._user
        return await call_next(request)


@pytest.fixture
def client(db_session):
    _seed_task(db_session, CFG)
    TaskExecutionRecordService(db_session).complete_record(
        "task_gate", 1, AGG, gate_result=evaluate_gate(CFG, AGG))
    db_session.add(EvaluationTask(
        id="task_plain", name="无门禁任务", evaluation_type="judge", status="pending",
        execution_count=0, team_code="DEFAULT"))
    db_session.commit()

    app = FastAPI()
    app.include_router(task_router, prefix="/api/v1")  # router 自带 /evaluation/tasks 前缀
    app.include_router(results_router, prefix="/api/v1")  # QE1-6：结果历史数据源
    app.add_middleware(_AuthStub, user={"user_code": "u", "org_code": "DEFAULT",
                                        "super_admin_flag": 0})
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_execution_detail_endpoint_returns_gate_result(client):
    resp = client.get("/api/v1/evaluation/tasks/task_gate/executions/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_number"] == 1
    assert body["status"] == "completed"
    assert body["gate_result"]["verdict"] == VERDICT_PASSED
    assert body["gate_result"]["checks"][0]["actual"] == 8.5


def test_execution_detail_404(client):
    assert client.get("/api/v1/evaluation/tasks/task_gate/executions/99").status_code == 404


def test_gate_endpoint_latest_execution(client):
    resp = client.get("/api/v1/evaluation/tasks/task_gate/gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gate_enabled"] is True
    assert body["verdict"] == VERDICT_PASSED
    assert body["gate_result"]["verdict"] == VERDICT_PASSED
    assert body["execution_number"] == 1


def test_gate_endpoint_task_without_gate(client):
    resp = client.get("/api/v1/evaluation/tasks/task_plain/gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gate_enabled"] is False
    assert body["verdict"] is None
    assert body["gate_result"] is None


def test_gate_endpoint_missing_task_404(client):
    assert client.get("/api/v1/evaluation/tasks/nope/gate").status_code == 404


def test_gate_endpoint_specified_execution(client, db_session):
    # 第 2 次执行：stopped，收尾落 error verdict
    db_session.query(TaskExecutionRecord).filter(
        TaskExecutionRecord.task_id == "task_gate",
        TaskExecutionRecord.execution_number == 2).delete()
    db_session.add(TaskExecutionRecord(id="exec_r2", task_id="task_gate",
                                       execution_number=2, status="stopped"))
    db_session.commit()
    TaskExecutionRecordService(db_session).stop_record("task_gate", 2)
    resp = client.get("/api/v1/evaluation/tasks/task_gate/gate?execution_number=2")
    body = resp.json()
    assert body["verdict"] == VERDICT_ERROR


def test_gate_routes_registered_for_api_key():
    """两查询端点进 API Key 注册表（default-deny 白名单），CLI 轮询可达"""
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1/gate") == scope_constants.TASKS_READ
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1/executions/3") == scope_constants.TASKS_READ
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1") is None  # 任务详情不在白名单


# ---------------- 4. M1 QE1-6：gate_config 配置贯通与历史判定回读 ----------------

GATE_CFG_BODY = {"mode": "all", "thresholds": [
    {"metric": "total_score", "op": ">=", "value": 8.0}]}


def test_create_task_persists_gate_config(client):
    """创建任务携带 gate_config 落库（前端门禁折叠区提交面）"""
    # success_rate 类型无待评测数据行要求（judge 需 evaluation_targets，与本测试无关）
    resp = client.post("/api/v1/evaluation/tasks", json={
        "name": "QE1-6 任务", "evaluation_type": "success_rate",
        "gate_config": GATE_CFG_BODY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    task_id = body["id"]
    # 编辑回显：config 端点带出 gate_config
    cfg = client.get(f"/api/v1/evaluation/tasks/{task_id}/config").json()
    assert cfg["gate_config"] == GATE_CFG_BODY


def test_update_task_can_disable_gate(client):
    """编辑任务把 gate_config 置 None → 关闭门禁（局部更新覆盖语义）"""
    task_id = client.post("/api/v1/evaluation/tasks", json={
        "name": "QE1-6 关闭", "evaluation_type": "success_rate",
        "gate_config": GATE_CFG_BODY}).json()["id"]
    resp = client.put(f"/api/v1/evaluation/tasks/{task_id}", json={"gate_config": None})
    assert resp.status_code == 200, resp.text
    cfg = client.get(f"/api/v1/evaluation/tasks/{task_id}/config").json()
    assert cfg["gate_config"] is None
    assert client.get(f"/api/v1/evaluation/tasks/{task_id}/gate").json()["gate_enabled"] is False


def test_history_summary_and_version_carry_gate_result(client):
    """结果页数据源：版本摘要（轻量）与单版端点都携带 gate_result（横幅/版本下拉渲染依据）"""
    summaries = client.get(
        "/api/v1/evaluation/results/task_gate/history",
        params={"include_result": False}).json()
    target = next(s for s in summaries if s["execution_number"] == 1)
    assert target["status"] == "completed"
    assert target["result"] is None  # 轻量摘要仍不带大列
    assert target["gate_result"]["verdict"] == VERDICT_PASSED

    version = client.get(
        "/api/v1/evaluation/results/task_gate/history/1").json()
    assert version["gate_result"]["verdict"] == VERDICT_PASSED
    assert version["gate_result"]["checks"][0]["actual"] == 8.5
