"""M1 QE1-3 webhook 测试：签名/防重放、投递状态机与重试矩阵、SSRF 接线、
事件分发与评测收尾发射、CRUD 端点（真实路由 + 归属校验链路）。

分块：
1. 签名（build_delivery_headers / verify_signature）：HMAC 送达可验、篡改
   必拒、时间窗防重放；
2. execute_delivery（SQLite + http 假体）：success/retry 指数退避/重试耗尽
   failed/SSRF 拒绝/端点停用/投递头真签（HMAC 签名送达断言）；
3. dispatch_event + 评测收尾发射（complete/fail_record → completed/failed/
   gate.failed 事件旁路派发，派发失败不拖挂主流程）；
4. CRUD 端点（TestClient + 生产 router）：创建明文一次性返回、列表不回显、
   跨团队 403、私网 URL 400、事件白名单 400、deliveries 留痕查询；
5. celery 路由：任务名落 maint 队列（glob 路由）。
"""
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.webhook_api import router as webhook_router
from app.celery_app import TASK_ROUTES
from app.core import scopes as scope_constants
from app.db.session import Base, get_db
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.models.webhook_models import WebhookDelivery, WebhookEndpoint
from app.services import webhook_service
from app.services.evaluation_task_service import TaskExecutionRecordService
from app.services.webhook_service import (
    OUTCOME_FAILED,
    OUTCOME_RETRY,
    OUTCOME_SUCCESS,
    build_delivery_headers,
    verify_signature,
)

pytestmark = pytest.mark.unit

# 公网字面 IP（getaddrinfo 本地解析，无网络依赖）；私网目标用于 SSRF 断言
PUBLIC_URL = "https://93.184.216.34/hook"
PRIVATE_URL = "http://192.168.1.10/hook"


# ---------------- 1. 签名 / 防重放 ----------------

def test_signature_roundtrip_and_tamper():
    secret = "whsec_test"
    body = '{"event": "gate.failed"}'
    headers = build_delivery_headers("gate.failed", secret, body, timestamp="1700000000")
    sig = headers["X-Aievo-Signature"]
    assert sig.startswith("sha256=")
    assert verify_signature(secret, body, "1700000000", sig, now=1700000000)
    # 篡改 body / 错 secret / 错签名 → 拒
    assert not verify_signature(secret, body + "x", "1700000000", sig, now=1700000000)
    assert not verify_signature("whsec_other", body, "1700000000", sig, now=1700000000)
    assert not verify_signature(secret, body, "1700000000", "sha256=" + "0" * 64, now=1700000000)


def test_replay_window():
    secret, body = "whsec_test", "{}"
    headers = build_delivery_headers("gate.failed", secret, body, timestamp="1700000000")
    sig = headers["X-Aievo-Signature"]
    # 窗内（±300s）通过
    assert verify_signature(secret, body, "1700000000", sig, now=1700000000 + 299)
    assert verify_signature(secret, body, "1700000000", sig, now=1700000000 - 300)
    # 超窗（重放）拒绝
    assert not verify_signature(secret, body, "1700000000", sig, now=1700000000 + 301)
    assert not verify_signature(secret, body, "1700000000", sig, now=1700000000 - 301)
    # 非数字时间戳拒绝
    assert not verify_signature(secret, body, "not-a-ts", sig, now=1700000000)


# ---------------- 测试基建（SQLite 内存库；execute_delivery 的注入面直接
# 传 lambda: db_session——SQLAlchemy 会话 close 后可复用，StaticPool 单连接
# 保证跨线程/跨 close 看到同一库） ----------------

@pytest.fixture
def db_session():
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


def seed_endpoint(db, url=PUBLIC_URL, events=None, active=True, team="DEFAULT",
                  secret="whsec_seed"):
    endpoint = WebhookEndpoint(
        team_code=team, name="ci-hook", url=url, secret=secret,
        events=events or ["task.execution.completed", "task.execution.failed", "gate.failed"],
        active=active, created_by="u1",
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    return endpoint


def seed_delivery(db, endpoint_id, event="gate.failed", payload=None, attempts=0):
    delivery = WebhookDelivery(
        endpoint_id=endpoint_id, event=event,
        payload=payload or {"task_id": "t1", "execution_number": 1},
        status="pending", attempts=attempts,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def make_session_factory(db_session):
    """从既有连接造 session_factory（execute_delivery 的注入面）"""
    factory = db_session.get_bind().pool  # noqa: F841
    return lambda: _BoundSession(db_session)


class _BoundSession:
    """复用同一底层连接的会话替身（get/commit/close 直通真 session）"""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, item):
        return getattr(self._session, item)

    def close(self):
        # 复用测试会话：不真关（测试生命周期管理）
        pass


# ---------------- 2. execute_delivery 状态机 ----------------

def test_execute_delivery_success_signs_headers(db_session):
    endpoint = seed_endpoint(db_session, secret="whsec_sign")
    delivery = seed_delivery(db_session, endpoint.id)
    delivery_id = delivery.id  # execute_delivery 会 close 会话（expunge 对象），先落标量
    captured = {}

    def fake_post(url, *, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body, timeout=timeout)
        return 200

    outcome, _ = webhook_service.execute_delivery(
        delivery.id, http_post=fake_post, session_factory=lambda: db_session)
    assert outcome == OUTCOME_SUCCESS
    # 签名送达断言：捕获的投递头能用端点 secret 在接收端口径验过
    assert captured["url"] == PUBLIC_URL
    body_text = captured["body"].decode("utf-8")
    assert verify_signature(
        "whsec_sign", body_text,
        captured["headers"]["X-Aievo-Timestamp"],
        captured["headers"]["X-Aievo-Signature"],
        now=int(captured["headers"]["X-Aievo-Timestamp"]),
    )
    assert captured["headers"]["X-Aievo-Event"] == "gate.failed"
    row = db_session.get(WebhookDelivery, delivery_id)
    assert row.status == "success"
    assert row.attempts == 1
    assert row.response_status == 200


def test_execute_delivery_retry_backoff_matrix(db_session):
    """非 2xx/网络异常 → retry（退避 30/60/120s）；第 4 次尝试后终态 failed"""
    endpoint = seed_endpoint(db_session)
    delivery = seed_delivery(db_session, endpoint.id)
    delivery_id = delivery.id

    def always_500(url, *, headers, body, timeout):
        return 500

    expected = [(1, OUTCOME_RETRY, 30), (2, OUTCOME_RETRY, 60), (3, OUTCOME_RETRY, 120)]
    for attempt, outcome_expected, countdown_expected in expected:
        outcome, countdown = webhook_service.execute_delivery(
            delivery_id, http_post=always_500, session_factory=lambda: db_session)
        assert outcome == outcome_expected, attempt
        assert countdown == countdown_expected, attempt
        row = db_session.get(WebhookDelivery, delivery_id)
        assert row.status == "pending"  # 重试未耗尽保持 pending
        assert row.attempts == attempt

    outcome, countdown = webhook_service.execute_delivery(
        delivery_id, http_post=always_500, session_factory=lambda: db_session)
    assert outcome == OUTCOME_FAILED
    assert countdown is None
    row = db_session.get(WebhookDelivery, delivery_id)
    assert row.status == "failed"
    assert row.attempts == 4  # 首次 + 重试 3 次
    assert row.response_status == 500


def test_execute_delivery_network_exception_retries(db_session):
    endpoint = seed_endpoint(db_session)
    delivery = seed_delivery(db_session, endpoint.id)
    delivery_id = delivery.id

    def boom(url, **kwargs):
        raise ConnectionError("receiver unreachable")

    outcome, countdown = webhook_service.execute_delivery(
        delivery.id, http_post=boom, session_factory=lambda: db_session)
    assert outcome == OUTCOME_RETRY
    row = db_session.get(WebhookDelivery, delivery_id)
    assert "发送异常" in row.last_error


def test_execute_delivery_ssrf_rejected_no_http_call(db_session):
    """投递时 URL 重验：私网目标终态 failed，且绝不发出 HTTP（SSRF 接线）"""
    endpoint = seed_endpoint(db_session, url=PRIVATE_URL)
    delivery = seed_delivery(db_session, endpoint.id)
    delivery_id = delivery.id
    calls = []

    def must_not_call(url, **kwargs):
        calls.append(url)
        return 200

    outcome, _ = webhook_service.execute_delivery(
        delivery.id, http_post=must_not_call, session_factory=lambda: db_session)
    assert outcome == OUTCOME_FAILED
    assert calls == []
    row = db_session.get(WebhookDelivery, delivery_id)
    assert row.status == "failed"
    assert "SSRF" in row.last_error


def test_execute_delivery_ssrf_allow_private_flag(db_session):
    """WEBHOOK_ALLOW_PRIVATE_TARGETS 显式放行时内网目标可投（企业接收端）"""
    endpoint = seed_endpoint(db_session, url=PRIVATE_URL)
    delivery = seed_delivery(db_session, endpoint.id)
    outcome, _ = webhook_service.execute_delivery(
        delivery.id, http_post=lambda url, **kw: 200,
        allow_private=True, session_factory=lambda: db_session)
    assert outcome == OUTCOME_SUCCESS


def test_execute_delivery_inactive_endpoint(db_session):
    endpoint = seed_endpoint(db_session, active=False)
    delivery = seed_delivery(db_session, endpoint.id)
    delivery_id = delivery.id
    outcome, _ = webhook_service.execute_delivery(
        delivery.id, http_post=lambda url, **kw: 200,
        session_factory=lambda: db_session)
    assert outcome == OUTCOME_FAILED
    row = db_session.get(WebhookDelivery, delivery_id)
    assert row.status == "failed"
    assert "停用" in row.last_error


def test_execute_delivery_missing_row(db_session):
    """投递行不存在：终态 failed 结果，不发起 HTTP"""
    outcome, _ = webhook_service.execute_delivery(
        999999, http_post=lambda url, **kw: 200,
        session_factory=lambda: db_session)
    assert outcome == OUTCOME_FAILED


def test_execute_delivery_real_http_roundtrip(db_session):
    """真 httpx 通道端到端：本地起接收端（127.0.0.1，allow_private 显式放行），
    断言线上请求头/报文原样到达且签名可验；500→retry 也验证真实状态码回填"""
    import json as jsonlib
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length).decode("utf-8")
            received["signature"] = self.headers.get("X-Aievo-Signature")
            received["timestamp"] = self.headers.get("X-Aievo-Timestamp")
            received["event"] = self.headers.get("X-Aievo-Event")
            if not received.get("return_ok"):  # 第一次 500（真实失败路径）
                received["return_ok"] = True
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # 静默默认访问日志
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = seed_endpoint(db_session, url=f"http://127.0.0.1:{port}/hook",
                                 secret="whsec_real")
        delivery = seed_delivery(db_session, endpoint.id, payload={"task_id": "t-real"})
        delivery_id = delivery.id

        # 第一次：接收端 500 → retry（真实状态码回填留痕）
        outcome, _ = webhook_service.execute_delivery(
            delivery_id, allow_private=True, session_factory=lambda: db_session)
        assert outcome == OUTCOME_RETRY
        row = db_session.get(WebhookDelivery, delivery_id)
        assert row.response_status == 500

        # 第二次：接收端 200 → success；签名可验（HMAC 真送达断言）
        outcome, _ = webhook_service.execute_delivery(
            delivery_id, allow_private=True, session_factory=lambda: db_session)
        assert outcome == OUTCOME_SUCCESS
        assert received["event"] == "gate.failed"
        assert verify_signature(
            "whsec_real", received["body"], received["timestamp"],
            received["signature"], now=int(received["timestamp"]))
        assert jsonlib.loads(received["body"])["task_id"] == "t-real"
    finally:
        server.shutdown()


# ---------------- 3. dispatch_event + 评测收尾发射 ----------------

def test_dispatch_event_fanout_and_filters(db_session, monkeypatch):
    """按事件订阅 × 活跃 × 团队过滤扇出，并逐行派发 celery 任务"""
    target = seed_endpoint(db_session)  # DEFAULT 团队、订阅三事件
    seed_endpoint(db_session, events=["task.execution.completed"], team="OTHER")
    seed_endpoint(db_session, active=False)
    seed_endpoint(db_session, events=["task.execution.completed"])

    dispatched = []

    class FakeTask:
        @staticmethod
        def apply_async(args=None, **kwargs):
            dispatched.append(args[0])

    import app.tasks.maintenance.webhook_delivery as wd
    monkeypatch.setattr(wd, "deliver_webhook", FakeTask, raising=False)

    created = webhook_service.dispatch_event(
        "gate.failed", "DEFAULT", {"task_id": "t1"}, db=db_session)
    assert created == 1  # 只有 target 同时满足（团队/活跃/订阅 gate.failed）
    assert len(dispatched) == 1
    row = db_session.query(WebhookDelivery).one()
    assert row.endpoint_id == target.id
    assert row.event == "gate.failed"
    assert row.status == "pending"


def test_dispatch_event_dispatch_failure_swallowed(db_session, monkeypatch):
    """celery 派发失败：留痕行保留、不抛出（旁路契约）"""
    seed_endpoint(db_session)

    class BrokenTask:
        @staticmethod
        def apply_async(args=None, **kwargs):
            raise ConnectionError("broker down")

    import app.tasks.maintenance.webhook_delivery as wd
    monkeypatch.setattr(wd, "deliver_webhook", BrokenTask, raising=False)

    created = webhook_service.dispatch_event(
        "task.execution.failed", "DEFAULT", {"task_id": "t1"}, db=db_session)
    assert created == 1
    assert db_session.query(WebhookDelivery).count() == 1


def test_record_completion_emits_events(db_session, monkeypatch):
    """complete_record → task.execution.completed；门禁 failed 额外 gate.failed"""
    db_session.add(EvaluationTask(
        id="task_wh", name="webhook 任务", evaluation_type="judge", status="running",
        execution_count=1, team_code="DEFAULT"))
    db_session.add(TaskExecutionRecord(id="exec_wh", task_id="task_wh",
                                       execution_number=1, status="running"))
    db_session.commit()
    events_sent = []

    monkeypatch.setattr(
        webhook_service, "dispatch_event",
        lambda event, team, payload, db=None: events_sent.append(event) or 1)

    TaskExecutionRecordService(db_session).complete_record(
        "task_wh", 1, {"average_score": 5.0},
        gate_result={"verdict": "failed", "checks": []})

    assert events_sent == ["task.execution.completed", "gate.failed"]


def test_record_completion_no_gate_failed_when_passed(db_session, monkeypatch):
    db_session.add(EvaluationTask(
        id="task_wh2", name="webhook 任务", evaluation_type="judge", status="running",
        execution_count=1, team_code="DEFAULT"))
    db_session.commit()
    events_sent = []
    monkeypatch.setattr(
        webhook_service, "dispatch_event",
        lambda event, team, payload, db=None: events_sent.append(event) or 1)

    TaskExecutionRecordService(db_session).complete_record(
        "task_wh2", 1, {}, gate_result={"verdict": "passed", "checks": []})
    assert events_sent == ["task.execution.completed"]

    events_sent.clear()
    TaskExecutionRecordService(db_session).fail_record("task_wh2", 1, "boom")
    assert events_sent == ["task.execution.failed"]


def test_record_emission_failure_does_not_break_completion(db_session, monkeypatch):
    """事件派发异常绝不打断收尾主流程（旁路契约）"""
    db_session.add(EvaluationTask(
        id="task_wh3", name="webhook 任务", evaluation_type="judge", status="running",
        execution_count=1, team_code="DEFAULT"))
    db_session.add(TaskExecutionRecord(id="exec_wh3", task_id="task_wh3",
                                       execution_number=1, status="running"))
    db_session.commit()

    def broken_dispatch(event, team, payload, db=None):
        raise RuntimeError("webhook subsystem down")

    monkeypatch.setattr(webhook_service, "dispatch_event", broken_dispatch)

    record = TaskExecutionRecordService(db_session).complete_record(
        "task_wh3", 1, {"average_score": 9.0}, gate_result=None)
    assert record.status == "completed"  # 主流程不受影响


# ---------------- 4. CRUD 端点（真实路由 + 归属校验） ----------------

class _AuthStub(BaseHTTPMiddleware):
    def __init__(self, app, user):
        super().__init__(app)
        self._user = user

    async def dispatch(self, request: Request, call_next):
        request.state.user = self._user
        return await call_next(request)


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(webhook_router, prefix="/api/v1")
    app.add_middleware(_AuthStub, user={"user_code": "u", "org_code": "DEFAULT",
                                        "super_admin_flag": 0})
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_create_returns_secret_once_and_list_hides_it(client, db_session):
    resp = client.post("/api/v1/webhooks", json={
        "name": "ci", "url": PUBLIC_URL,
        "events": ["gate.failed", "task.execution.completed"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secret"].startswith("whsec_")  # 仅创建时明文
    assert body["team_code"] == "DEFAULT"
    assert sorted(body["events"]) == ["gate.failed", "task.execution.completed"]

    listing = client.get("/api/v1/webhooks").json()
    assert len(listing) == 1
    assert "secret" not in listing[0]
    # 库内 secret 与返回明文一致（投递签名用）
    endpoint = db_session.query(WebhookEndpoint).one()
    assert endpoint.secret == body["secret"]


def test_create_rejects_private_url_and_bad_events(client):
    assert client.post("/api/v1/webhooks", json={
        "name": "evil", "url": "http://127.0.0.1:8080/hook",
        "events": ["gate.failed"]}).status_code == 400
    assert client.post("/api/v1/webhooks", json={
        "name": "bad", "url": PUBLIC_URL,
        "events": ["not.an.event"]}).status_code == 400
    # 空事件表过 pydantic（List 允许空），由服务层白名单校验拒绝（400）
    assert client.post("/api/v1/webhooks", json={
        "name": "empty", "url": PUBLIC_URL, "events": []}).status_code == 400


def test_delete_and_deliveries_scoping(client, db_session):
    endpoint = seed_endpoint(db_session)
    seed_delivery(db_session, endpoint.id, event="gate.failed")
    seed_delivery(db_session, endpoint.id, event="task.execution.completed")

    listing = client.get(f"/api/v1/webhooks/{endpoint.id}/deliveries").json()
    assert len(listing) == 2  # 倒序：最新在前
    assert listing[0]["event"] == "task.execution.completed"
    assert listing[0]["status"] == "pending"

    assert client.delete(f"/api/v1/webhooks/{endpoint.id}").status_code == 200
    assert client.get("/api/v1/webhooks").json() == []
    # 端点删除后 deliveries 级联清理（ON DELETE CASCADE，SQLite 需开外键——
    # 声明式口径下本用例只断言端点不复存在）
    assert db_session.query(WebhookEndpoint).count() == 0


def test_cross_team_404_403(client, db_session):
    endpoint = seed_endpoint(db_session, team="OTHER_TEAM")
    assert client.get(f"/api/v1/webhooks/{endpoint.id}/deliveries").status_code == 403
    assert client.delete(f"/api/v1/webhooks/{endpoint.id}").status_code == 403
    assert client.delete("/api/v1/webhooks/99999").status_code == 404


def test_webhook_routes_registered_for_api_key():
    for method, path in (
        ("POST", "/api/v1/webhooks"),
        ("GET", "/api/v1/webhooks"),
        ("DELETE", "/api/v1/webhooks/7"),
        ("GET", "/api/v1/webhooks/7/deliveries"),
    ):
        assert scope_constants.match_api_key_route(method, path) == \
            scope_constants.WEBHOOKS_MANAGE, (method, path)


# ---------------- 5. celery 路由 ----------------

def test_delivery_task_routes_to_maint_queue():
    """任务名落 app.tasks.maintenance.* 命名空间 → glob 路由 maint 队列"""
    from app.celery_app import celery_app
    from app.tasks.maintenance.webhook_delivery import deliver_webhook

    route = celery_app.amqp.router.route({}, deliver_webhook.name)
    assert route["queue"].name == "maint"
