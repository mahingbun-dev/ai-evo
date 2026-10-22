"""M1 QE1-4 端到端 scope 化测试：API Key 全链路触发运行与读取结果。

链路是**真实**的：TestClient → 真 AuthMiddleware（API Key 分支/JWT 分支）
→ 真 task_router/results_router/api_key_router → sqlite 库 → 归属校验。
只有 Celery 提交面打桩（submit_task 不真投队列）。

覆盖：
1. 持 tasks:run+tasks:read+results:read 的 Key：run 触发 200、gate 轮询 200、
   results 三端点 200；
2. scope 最小化：只持 tasks:read 的 Key 触发 run → 403；未注册端点
   （任务详情/删除/api-keys 管理）→ 403/401；
3. 浏览器 JWT 路径不受影响：同端点 JWT 调用照常 200；
4. api-keys 管理端点（登录态）：创建明文一次性返回、列表不回显、吊销幂等、
   吊销后 Key 认证 401；
5. 跨团队：Key 所属用户与任务异团 → 403（归属校验对 API Key 同口径）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.api_key_api import router as api_key_router
from app.api.evaluation_task_api import results_router, router as task_router
from app.api.webhook_api import router as webhook_router  # noqa: F401 注册表 import 期登记
from app.core import scopes as scope_constants
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.evaluation_task_service import TaskExecutionRecordService
from app.services.gate_service import evaluate_gate
from app.services.jwt_service import create_token
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit

GATE_CFG = {"enabled": True, "mode": "all", "thresholds": [
    {"metric": "total_score", "op": ">=", "value": 8.0}]}
AGG = {"total": 1, "success_count": 1, "success_rate": 1.0,
       "average_score": 8.5, "dimension_scores": {}}


class FakeScheduler:
    """Celery 提交面桩：不真投队列"""

    def submit_task(self, task_id):
        return True


@pytest.fixture
def stack(monkeypatch):
    """真中间件 + 真 router + sqlite 单库；返回 (client, 工具函数)"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)
    monkeypatch.setattr(
        "app.services.task_scheduler.get_task_scheduler", lambda: FakeScheduler())

    with TestSession() as db:
        db.add(User(user_code="ci-user", username="ci", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="other-user", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        db.add(EvaluationTask(
            id="task_ci", name="CI 门禁任务", evaluation_type="judge", status="pending",
            team_code="DEFAULT", gate_config=GATE_CFG, execution_count=1))
        db.add(TaskExecutionRecord(id="exec_ci", task_id="task_ci",
                                   execution_number=1, status="running"))
        db.commit()
        # 第 1 次执行已完成（含门禁判定）——results/gate 读取面有数据可查
        TaskExecutionRecordService(db).complete_record(
            "task_ci", 1, AGG, gate_result=evaluate_gate(GATE_CFG, AGG))

    app = FastAPI()
    app.include_router(task_router, prefix="/api/v1")
    app.include_router(results_router, prefix="/api/v1")
    app.include_router(api_key_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()

    jwt = create_token("ci-user", "ci")

    def make_key(scopes_granted, user_code="ci-user"):
        """经管理端点创建一枚真 Key（以 user_code 的登录态），返回明文 token"""
        user_jwt = jwt if user_code == "ci-user" else create_token(user_code, user_code)
        with TestClient(app) as client:  # with：触发 lifespan（无需真启停）
            resp = client.post("/api/v1/api-keys", headers={
                "Authorization": f"Bearer {user_jwt}"}, json={
                "name": "test-key", "scopes": scopes_granted})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    return SimpleNamespaceLike(app=app, jwt=jwt, make_key=make_key,
                              session=TestSession)


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------- 1. 全链路：持满 scope 的 Key 触发运行 + 读结果 ----------------

def test_api_key_full_chain_run_gate_results(stack, client):
    token = stack.make_key(["tasks:run", "tasks:read", "results:read"])
    # 触发运行（celery 提交面已打桩；任务置 running + 执行记录创建）
    resp = client.post("/api/v1/evaluation/tasks/task_ci/run", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"

    # 门禁轮询：run 后最新执行（第 2 次）running、verdict 未出（CLI 继续轮询语义）
    resp = client.get("/api/v1/evaluation/tasks/task_ci/gate", headers=auth(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert resp.json()["execution_number"] == 2
    assert resp.json()["verdict"] is None
    # 指定执行序号查历史判定（第 1 次已完成 → passed）
    resp = client.get("/api/v1/evaluation/tasks/task_ci/gate?execution_number=1",
                      headers=auth(token))
    assert resp.json()["verdict"] == "passed"

    # 结果读取三端点
    assert client.get("/api/v1/evaluation/results/task_ci", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/evaluation/results/task_ci/history", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/evaluation/results/task_ci/history/1", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/evaluation/results/task_ci/dimension-trend", headers=auth(token)).status_code == 200


def test_api_key_last_used_at_updated(stack, client):
    token = stack.make_key(["tasks:read"])
    client.get("/api/v1/evaluation/tasks/task_ci/gate", headers=auth(token))
    with stack.session() as db:
        from app.models.api_key_models import ApiKey
        key = db.query(ApiKey).one()
        assert key.last_used_at is not None


# ---------------- 2. scope 最小化（default-deny 白名单外全拒） ----------------

def test_api_key_without_run_scope_rejected(stack, client):
    token = stack.make_key(["tasks:read"])  # 无 tasks:run
    resp = client.post("/api/v1/evaluation/tasks/task_ci/run", headers=auth(token))
    assert resp.status_code == 403
    assert "tasks:run" in resp.json()["message"]


def test_api_key_cannot_touch_unregistered_endpoints(stack, client):
    token = stack.make_key(["tasks:run", "tasks:read", "results:read"])
    # 任务详情/删除/api-keys 管理均不在 API Key 白名单
    assert client.get("/api/v1/evaluation/tasks/task_ci", headers=auth(token)).status_code == 403
    assert client.delete("/api/v1/evaluation/tasks/task_ci", headers=auth(token)).status_code == 403
    assert client.get("/api/v1/api-keys", headers=auth(token)).status_code == 403
    assert client.post("/api/v1/api-keys", headers=auth(token), json={
        "name": "x", "scopes": ["tasks:read"]}).status_code == 403


def test_api_key_export_stays_login_only(stack, client):
    """results 的 export 是写操作（生成+上传文档），不进 API Key 白名单"""
    token = stack.make_key(["results:read"])
    resp = client.post("/api/v1/evaluation/results/task_ci/history/1/export",
                       headers=auth(token), json={"format": "json"})
    assert resp.status_code == 403  # 未注册端点：default-deny


# ---------------- 3. 浏览器 JWT 路径不受影响 ----------------

def test_jwt_path_still_works(stack, client):
    """同端点 JWT 登录态照常可用（require_scope 对 JWT 放行）"""
    resp = client.post("/api/v1/evaluation/tasks/task_ci/run", headers=auth(stack.jwt))
    assert resp.status_code == 200
    assert client.get("/api/v1/evaluation/tasks/task_ci/gate", headers=auth(stack.jwt)).status_code == 200
    assert client.get("/api/v1/evaluation/results/task_ci", headers=auth(stack.jwt)).status_code == 200
    assert client.get("/api/v1/evaluation/tasks/task_ci", headers=auth(stack.jwt)).status_code == 200


# ---------------- 4. api-keys 管理端点（登录态） ----------------

def test_api_keys_crud_lifecycle(stack, client):
    # 创建：明文 token 一次性返回，格式合法
    resp = client.post("/api/v1/api-keys", headers=auth(stack.jwt), json={
        "name": "ci-key", "scopes": ["tasks:run", "tasks:read"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"].startswith("aievo_sk_")
    assert sorted(body["scopes"]) == ["tasks:read", "tasks:run"]
    key_id = body["id"]

    # 列表：本人 Key、不含 token 明文（含 key_prefix 展示）
    listing = client.get("/api/v1/api-keys", headers=auth(stack.jwt)).json()
    assert len(listing) == 1
    assert "token" not in listing[0]
    assert listing[0]["key_prefix"] == body["token"][len("aievo_sk_"):len("aievo_sk_") + 8]

    # 新 Key 立即可用（认证链路）
    assert client.get("/api/v1/evaluation/tasks/task_ci/gate",
                      headers=auth(body["token"])).status_code == 200

    # 吊销（幂等）→ 再用该 Key 认证 401
    assert client.delete(f"/api/v1/api-keys/{key_id}", headers=auth(stack.jwt)).status_code == 200
    assert client.delete(f"/api/v1/api-keys/{key_id}", headers=auth(stack.jwt)).status_code == 200
    assert client.get("/api/v1/evaluation/tasks/task_ci/gate",
                      headers=auth(body["token"])).status_code == 401


def test_api_keys_validation_and_isolation(stack, client):
    assert client.post("/api/v1/api-keys", headers=auth(stack.jwt), json={
        "name": "bad", "scopes": ["bogus:scope"]}).status_code == 400
    assert client.post("/api/v1/api-keys", headers=auth(stack.jwt), json={
        "name": "x", "scopes": ["tasks:read"], "expires_at": "2000-01-01T00:00:00Z"}
    ).status_code == 400
    # 归属隔离：他人 Key 按 404 口径（不泄露存在性）
    other_jwt = create_token("other-user", "other")
    mine = client.post("/api/v1/api-keys", headers=auth(stack.jwt), json={
        "name": "mine", "scopes": ["tasks:read"]}).json()
    assert client.delete(f"/api/v1/api-keys/{mine['id']}",
                         headers=auth(other_jwt)).status_code == 404
    # 未认证拒绝
    assert client.get("/api/v1/api-keys").status_code == 401


# ---------------- 5. 跨团队归属（API Key 按所属用户身份判团） ----------------

def test_api_key_cross_team_denied(stack, client):
    """Key 所属用户（OTHER 团队）访问 DEFAULT 团队任务：认证过、scope 过，
    仍被对象级归属校验拦下（403）——三层防线各司其职"""
    token = stack.make_key(["tasks:read", "tasks:run"], user_code="other-user")
    resp = client.get("/api/v1/evaluation/tasks/task_ci/gate", headers=auth(token))
    assert resp.status_code == 403
    assert "跨团队" in resp.json()["detail"] or "无权" in resp.json()["detail"]
    resp = client.post("/api/v1/evaluation/tasks/task_ci/run", headers=auth(token))
    assert resp.status_code == 403


# ---------------- 注册表契约（与路由定义同源） ----------------

def test_scope_registry_matches_qe14_api_list():
    """方案 §M1 QE1-4 新 API 清单：run=tasks:run；gate/executions=tasks:read；
    results GET=results:read；api-keys 不进注册表（登录态专用）"""
    expect = {
        ("POST", "/api/v1/evaluation/tasks/{task_id}/run"): scope_constants.TASKS_RUN,
        ("GET", "/api/v1/evaluation/tasks/{task_id}/gate"): scope_constants.TASKS_READ,
        ("GET", "/api/v1/evaluation/tasks/{task_id}/executions/{execution_number}"): scope_constants.TASKS_READ,
        ("GET", "/api/v1/evaluation/results/{task_id}"): scope_constants.RESULTS_READ,
        ("GET", "/api/v1/evaluation/results/{task_id}/history"): scope_constants.RESULTS_READ,
        ("GET", "/api/v1/evaluation/results/{task_id}/history/{execution_number}"): scope_constants.RESULTS_READ,
        ("GET", "/api/v1/evaluation/results/{task_id}/dimension-trend"): scope_constants.RESULTS_READ,
        ("POST", "/api/v1/webhooks"): scope_constants.WEBHOOKS_MANAGE,
    }
    for (method, path), scope in expect.items():
        assert scope_constants.match_api_key_route(method, path) == scope, (method, path)
    # api-keys 管理：API Key 不可达（default-deny）
    for method in ("POST", "GET"):
        assert scope_constants.match_api_key_route(method, "/api/v1/api-keys") is None
    assert scope_constants.match_api_key_route("DELETE", "/api/v1/api-keys/1") is None
    # results export（写操作）不在白名单
    assert scope_constants.match_api_key_route(
        "POST", "/api/v1/evaluation/results/t/history/1/export") is None
