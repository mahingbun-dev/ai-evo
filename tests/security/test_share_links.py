"""分享链接安全测试（M5 QE5-4，报告外发——新对外攻击面专项）

链路是真实的：TestClient → 真 AuthMiddleware（白名单放行 /share）→
share 管理面（鉴权）/share 公开报告面（免鉴权）→ sqlite。

覆盖方案 §5.4/§5.5 口径：
1. token 形态与存储：``sh_`` 前缀 urlsafe（≥128bit 熵）、库内只存 sha256
   哈希、明文仅创建响应返回一次（列表永不回显）；
2. 免登录可看：无 Authorization 头 GET 公开报告 200；
3. 吊销即失效 / 过期即失效（M5 验收口径）；
4. 限流：同 IP 第 11 次/分钟 429（Redis 固定窗口）；
5. 枚举防护：不存在 / 已吊销 / 已过期三种 token 响应完全一致（404 同体）；
6. 越权：viewer 创建/吊销 403；跨团队创建/列表/吊销 403；
7. 参数校验：非法有效期/范围/执行次数 400；
8. 审计：创建落 CREATE、公开访问落 VIEW（匿名 user_code 空、记 IP）；
9. 范围与执行选择：summary 摘除行级 items；跟随最新执行/锁定执行次数。
"""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.evaluation_task_api import router as task_router
from app.api.share_api import manage_router, public_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.share_models import ShareLink
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.jwt_service import create_token
from app.services.share_link_service import RATE_LIMIT_PER_MINUTE
from app.services.stats_service import BEIJING_TZ, beijing_now
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit  # W8 分类：unit（sqlite/FakeRedis 替身，无真实网络）


@pytest.fixture
def stack(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)
    # log_operation 独立 session 语义同生产：换绑到同库 factory
    monkeypatch.setattr("app.services.audit_service.SessionLocal", TestSession)
    # 公开端点限流用的 Redis：每测试独立 FakeRedis（隔离计数）
    rate_redis = FakeRedis()
    monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client",
                        lambda: rate_redis)

    with TestSession() as db:
        db.add(User(user_code="eng", username="eng", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="viewer", username="viewer", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT",
                    role="viewer"))
        db.add(User(user_code="other", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        db.commit()

    app = FastAPI()
    app.include_router(task_router, prefix="/api/v1")
    app.include_router(manage_router, prefix="/api/v1")
    app.include_router(public_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return type("Stack", (), {
        "app": app, "session": TestSession, "rate_redis": rate_redis,
        "jwt_eng": create_token("eng", "eng"),
        "jwt_viewer": create_token("viewer", "viewer"),
        "jwt_other": create_token("other", "other"),
    })()


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


@pytest.fixture
def task_with_result(stack, client):
    """真实任务（API 创建）+ 两次执行记录（第 2 次为最新，result 含 items）"""
    resp = client.post("/api/v1/evaluation/tasks",
                       headers={"Authorization": f"Bearer {stack.jwt_eng}"},
                       json={"name": "分享被测任务", "evaluation_type": "success_rate",
                             "success_rate_config": {"expected_field": "answer",
                                                     "mode": "contains"},
                             "judge_count": 0})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    with stack.session() as db:
        for n in (1, 2):
            db.add(TaskExecutionRecord(
                id=f"rec_{n}", task_id=task_id, execution_number=n,
                status="completed",
                result={"total": 10, "success_count": 8, "failed_count": 2,
                        "average_score": 7.5,
                        "dimension_scores": {"准确性": {"average": 7.5}},
                        "items": [{"index": 0, "score": 8, "user_input": "q0",
                                   "model_response": "a0", "judge_feedback": "ok"}]},
                started_at=beijing_now() - timedelta(hours=n),
                completed_at=beijing_now() - timedelta(hours=n - 1)))
        db.commit()
    return task_id


def _auth(stack, who="eng"):
    tokens = {"eng": stack.jwt_eng, "viewer": stack.jwt_viewer, "other": stack.jwt_other}
    return {"Authorization": f"Bearer {tokens[who]}"}


def _create_link(client, stack, task_id, **overrides):
    body = {"row_scope": "all", **overrides}
    body = {k: v for k, v in body.items() if v is not None or k == "expires_days"}
    resp = client.post(f"/api/v1/evaluation/tasks/{task_id}/share-links",
                       headers=_auth(stack), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ==================== token 形态与存储 ====================

class TestTokenHandling:
    def test_token_form_and_hash_only_storage(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result)
        token = created["token"]
        # 形态：sh_ 前缀 + urlsafe（24 字节 = 192bit 熵 ≥ 方案 128bit 下限）
        assert token.startswith("sh_")
        assert len(token) >= 30
        # 库内只存 sha256 hex，明文不落库
        with stack.session() as db:
            link = db.query(ShareLink).filter_by(id=created["link"]["id"]).first()
            assert len(link.token_hash) == 64
            assert token not in (link.token_hash or "")
            import hashlib
            assert link.token_hash == hashlib.sha256(token.encode()).hexdigest()

    def test_plaintext_token_returned_only_once(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result)
        listing = client.get(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack)).json()
        assert len(listing) == 1
        assert "token" not in listing[0]
        assert listing[0]["active"] is True
        assert listing[0]["row_scope"] == "all"


# ==================== 免登录读取与范围/执行选择 ====================

class TestPublicReport:
    def test_no_auth_header_200(self, client, stack, task_with_result):
        # 不带任何凭证：中间件白名单放行，端点可达
        created = _create_link(client, stack, task_with_result)
        resp = client.get(f"/api/v1/share/{created['token']}/report")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task"]["id"] == task_with_result
        assert body["result"]["average_score"] == 7.5
        # 跟随最新执行：execution_number=2
        assert body["execution_number"] == 2
        assert "items" in body["result"]

    def test_summary_scope_strips_rows(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result, row_scope="summary")
        body = client.get(f"/api/v1/share/{created['token']}/report").json()
        assert body["row_scope"] == "summary"
        assert "items" not in body["result"]
        # 聚合保留
        assert body["result"]["average_score"] == 7.5

    def test_locked_execution_number(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result, execution_number=1)
        body = client.get(f"/api/v1/share/{created['token']}/report").json()
        assert body["execution_number"] == 1


# ==================== 失效路径（过期/吊销/枚举） ====================

class TestInvalidation:
    def test_revoked_immediately_404(self, client, stack, task_with_result):
        # M5 验收：吊销即失效
        created = _create_link(client, stack, task_with_result)
        link_id = created["link"]["id"]
        resp = client.delete(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links/{link_id}",
            headers=_auth(stack))
        assert resp.status_code == 200
        assert resp.json()["active"] is False
        assert client.get(f"/api/v1/share/{created['token']}/report").status_code == 404

    def test_expired_404(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result, expires_days=1)
        with stack.session() as db:
            link = db.query(ShareLink).filter_by(id=created["link"]["id"]).first()
            link.expires_at = beijing_now() - timedelta(minutes=1)
            db.commit()
        assert client.get(f"/api/v1/share/{created['token']}/report").status_code == 404

    def test_enumeration_undifferentiated_responses(self, client, stack,
                                                    task_with_result):
        # 不存在 / 已吊销 / 已过期三种 token：状态码与响应体完全一致
        revoked = _create_link(client, stack, task_with_result)
        client.delete(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links/{revoked['link']['id']}",
            headers=_auth(stack))
        expired = _create_link(client, stack, task_with_result, expires_days=1)
        with stack.session() as db:
            row = db.query(ShareLink).filter_by(id=expired["link"]["id"]).first()
            row.expires_at = beijing_now() - timedelta(minutes=1)
            db.commit()

        unknown = client.get("/api/v1/share/sh_nope/report")
        got_revoked = client.get(f"/api/v1/share/{revoked['token']}/report")
        got_expired = client.get(f"/api/v1/share/{expired['token']}/report")
        assert unknown.status_code == got_revoked.status_code == got_expired.status_code == 404
        assert unknown.json() == got_revoked.json() == got_expired.json()


# ==================== 限流 ====================

class TestRateLimit:
    def test_11th_request_in_window_429(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result)
        url = f"/api/v1/share/{created['token']}/report"
        codes = [client.get(url).status_code for _ in range(RATE_LIMIT_PER_MINUTE)]
        assert codes == [200] * RATE_LIMIT_PER_MINUTE
        # 同 IP 窗口内第 11 次 → 429（有效 token 也拦）
        assert client.get(url).status_code == 429
        # 无效 token 同样先过限流闸（第 12 次，仍 429）
        assert client.get("/api/v1/share/sh_nope/report").status_code == 429

    def test_limit_key_window_expiry(self, client, stack, task_with_result):
        # 窗口过期后额度恢复（FakeRedis.advance 推进 TTL）
        created = _create_link(client, stack, task_with_result)
        url = f"/api/v1/share/{created['token']}/report"
        for _ in range(RATE_LIMIT_PER_MINUTE):
            client.get(url)
        assert client.get(url).status_code == 429
        stack.rate_redis.advance(61)
        assert client.get(url).status_code == 200


# ==================== 越权与参数校验 ====================

class TestAccessControl:
    def test_viewer_cannot_create_or_revoke(self, client, stack, task_with_result):
        assert client.post(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack, "viewer"), json={}).status_code == 403
        created = _create_link(client, stack, task_with_result)
        assert client.delete(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links/{created['link']['id']}",
            headers=_auth(stack, "viewer")).status_code == 403
        # viewer 列表可见（读路径与结果页一致，仅禁写）
        assert client.get(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack, "viewer")).status_code == 200

    def test_cross_team_invisible(self, client, stack, task_with_result):
        # 异团队：创建/列表/吊销全 403（对象级归属校验）
        assert client.post(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack, "other"), json={}).status_code == 403
        assert client.get(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack, "other")).status_code == 403
        created = _create_link(client, stack, task_with_result)
        assert client.delete(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links/{created['link']['id']}",
            headers=_auth(stack, "other")).status_code == 403

    def test_invalid_params_400(self, client, stack, task_with_result):
        # 有效期只支持 1/7/30/永久
        assert client.post(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack), json={"expires_days": 5}).status_code == 400
        # 范围只支持 all/summary
        assert client.post(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack), json={"row_scope": "everything"}).status_code == 400
        # 锁定不存在的执行次数
        assert client.post(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links",
            headers=_auth(stack), json={"execution_number": 99}).status_code == 400

    def test_revoke_unknown_link_404(self, client, stack, task_with_result):
        assert client.delete(
            f"/api/v1/evaluation/tasks/{task_with_result}/share-links/shl_nope",
            headers=_auth(stack)).status_code == 404


# ==================== 审计 ====================

class TestAuditing:
    def test_create_and_view_audited(self, client, stack, task_with_result):
        created = _create_link(client, stack, task_with_result)
        resp = client.get(f"/api/v1/share/{created['token']}/report")
        assert resp.status_code == 200
        with stack.session() as db:
            logs = db.query(OperationLog).filter(
                OperationLog.resource_type == "share_link").all()
        actions = {log.action: log for log in logs}
        # 创建落 CREATE（带创建人）；公开访问落 VIEW（匿名 user_code 空、记 IP）
        assert "CREATE" in actions
        assert actions["CREATE"].user_code == "eng"
        assert actions["CREATE"].resource_id == created["link"]["id"]
        assert "VIEW" in actions
        assert actions["VIEW"].user_code in ("", None)
        assert actions["VIEW"].ip_address  # testclient 也有直连地址
