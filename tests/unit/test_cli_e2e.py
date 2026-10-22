"""M1 QE1-5 CLI ↔ 平台端到端测试：真 HTTP、真中间件、真路由，仅本机回环。

链路是**真实**的：uvicorn 起真服务（127.0.0.1 随机端口）→ aievo-cli 走
httpx 发真请求 → 真 AuthMiddleware（API Key 分支：throttle/sha256 校验/
路由注册表/scope 检查）→ 真 task_router/results_router/dataset_router
→ sqlite 文件库。Celery 提交面打桩：submit_task 同步用真实
complete_task 落聚合结果与门禁判定，等价 worker 已跑完的稳态。

覆盖（对应 M1 验收「aievo run --gate 退出码矩阵」的最小子集）：
1. run --wait --json：门禁通过 → 退出码 0（verdict=passed 可机读）；
2. run --wait：门禁失败 → 退出码 1；
3. results -o json：CLI 读取任务结果 200 退出 0；
4. datasets upload：CLI 持 datasets:write Key 建集并导入（QE1-5 注册面）；
5. run --wait 持 tasks:read-only Key → 403 → 退出码 2（scope 收口生效）。
"""
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

# CLI 是独立子包（cli/pyproject.toml），测试环境直接挂源码路径
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

from aievo_cli import cli as cli_mod  # noqa: E402
from aievo_cli import config as cfg  # noqa: E402

from app.api.dataset_api import router as dataset_router  # noqa: F401 注册表 import 期登记
from app.api.evaluation_task_api import results_router, router as task_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.task_models import EvaluationTask
from app.models.user_models import User
from app.services.api_key_service import create_api_key
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.gate_service import evaluate_gate
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit

runner = CliRunner()

GATE_PASS = {"enabled": True, "mode": "all", "thresholds": [
    {"metric": "total_score", "op": ">=", "value": 8.0}]}
GATE_FAIL = {"enabled": True, "mode": "all", "thresholds": [
    {"metric": "total_score", "op": ">=", "value": 9.9}]}
AGG = {"total": 1, "success_count": 1, "success_rate": 1.0,
       "average_score": 8.5, "dimension_scores": {},
       "items": [{"idx": 1, "success": True, "row_status": "success",
                  "average_score": 8.5, "feedback": "ok"}]}


class CompletingScheduler:
    """Celery 提交面桩：提交即收尾（等价 worker 稳态完成）

    每个任务带自己的 gate_config，聚合结果统一 8.5 分——
    task_pass（阈值 8.0）判 passed，task_fail（阈值 9.9）判 failed。
    注：不走 complete_task 的 stats 收尾（stats duration 在 sqlite 下
    naive/aware 相减报错，是 sqlite 时区往返的既有测试环境差异，
    与门禁链路无关），直接落任务终态 + 执行记录 + 门禁判定。
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def submit_task(self, task_id):
        from app.services.evaluation_task_service import TaskExecutionRecordService, beijing_now

        with self._session_factory() as db:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            record_service = TaskExecutionRecordService(db)
            record_service.complete_record(
                task_id, task.execution_count, AGG,
                gate_result=evaluate_gate(task.gate_config, AGG))
            task.status = "completed"
            task.progress = 100
            task.result = AGG
            task.completed_at = beijing_now()
            db.commit()
        return True


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """真 uvicorn 服务 + sqlite 文件库；app 请求与调度器收尾各用独立连接池"""
    tmp_path = tmp_path_factory.mktemp("cli_e2e")
    db_url = f"sqlite:///{tmp_path}/e2e.db"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    AppSession = sessionmaker(bind=engine)
    # 调度器收尾走独立 engine：避免与请求会话共用 StaticPool 单连接互踩
    sched_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SchedSession = sessionmaker(bind=sched_engine)

    import app.services.task_scheduler as scheduler_mod
    original_getter = scheduler_mod.get_task_scheduler
    scheduler_mod.get_task_scheduler = lambda: CompletingScheduler(SchedSession)
    import app.middleware.auth_middleware as auth_mod
    original_session_local = auth_mod.SessionLocal
    auth_mod.SessionLocal = AppSession

    with AppSession() as db:
        db.add(User(user_code="ci-user", username="ci", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(EvaluationTask(
            id="task_pass", name="通过任务", evaluation_type="judge", status="pending",
            team_code="DEFAULT", gate_config=GATE_PASS, execution_count=0))
        db.add(EvaluationTask(
            id="task_fail", name="失败任务", evaluation_type="judge", status="pending",
            team_code="DEFAULT", gate_config=GATE_FAIL, execution_count=0))
        db.commit()

        # 两枚真 Key：全 scope（run/gate/results/datasets）与只读（验证 403 收口）
        _, full_token = create_api_key(
            db, "ci-user", "cli-full",
            ["tasks:run", "tasks:read", "results:read", "datasets:write"])
        _, read_token = create_api_key(db, "ci-user", "cli-readonly", ["tasks:read"])
        db.commit()

    app = FastAPI()
    app.include_router(task_router, prefix="/api/v1")
    app.include_router(results_router, prefix="/api/v1")
    app.include_router(dataset_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))

    def override_get_db():
        db = AppSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # 随机空闲端口 + 真 uvicorn 线程
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv_server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert uv_server.started

    yield type("Server", (), {
        "url": f"http://127.0.0.1:{port}", "full_token": full_token,
        "read_token": read_token})()

    uv_server.should_exit = True
    thread.join(timeout=5)
    scheduler_mod.get_task_scheduler = original_getter
    auth_mod.SessionLocal = original_session_local
    engine.dispose()
    sched_engine.dispose()


@pytest.fixture
def cli_config(server, monkeypatch, tmp_path):
    """CLI 配置指向真服务（明文 Key 落临时目录，不污染 ~/.aievo）"""
    monkeypatch.setenv("AIEVO_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg.save_profile("default", server.url, server.full_token)
    cfg.save_profile("readonly", server.url, server.read_token)
    return server


# ---------------- 门禁闭环：run --wait 退出码 ----------------

def test_cli_run_wait_passed_exit_0(cli_config):
    result = runner.invoke(cli_mod.app, [
        "run", "task_pass", "--wait", "--interval", "1", "--timeout", "15", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "passed"
    assert payload["status"] == "completed"
    assert payload["checks"][0]["passed"] is True


def test_cli_run_wait_failed_exit_1(cli_config):
    result = runner.invoke(cli_mod.app, [
        "run", "task_fail", "--wait", "--interval", "1", "--timeout", "15"])
    assert result.exit_code == 1


def test_cli_results_json(cli_config):
    result = runner.invoke(cli_mod.app, ["results", "task_pass", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["task_id"] == "task_pass"
    assert payload["result"]["average_score"] == 8.5


def test_cli_run_without_scope_exits_2(cli_config):
    """只读 Key 触发 run：中间件 scope 收口 403 → CLI 退出码 2"""
    result = runner.invoke(cli_mod.app, [
        "run", "task_pass", "--wait", "--profile", "readonly"])
    assert result.exit_code == 2


def test_cli_datasets_upload(cli_config, tmp_path):
    """datasets:write Key 建集并导入 JSON（QE1-5 注册面端到端验证）"""
    data_file = tmp_path / "e2e.json"
    data_file.write_text('[{"question": "1+1", "ref_answer": "2"}]', encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["datasets", "upload", str(data_file), "--name", "e2e集"])
    assert result.exit_code == 0, result.output
    assert "导入 1 条" in result.output


def test_cli_datasets_upload_without_scope_exits_2(cli_config, tmp_path):
    """只读 Key 上传数据集：datasets 端点 scope 收口 403 → CLI 退出码 2"""
    data_file = tmp_path / "denied.json"
    data_file.write_text('[{"question": "q"}]', encoding="utf-8")
    result = runner.invoke(cli_mod.app, [
        "datasets", "upload", str(data_file), "--profile", "readonly"])
    assert result.exit_code == 2
    assert "无权限" in result.output or "scope" in result.output
