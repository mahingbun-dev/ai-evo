"""M6 QE6-3 安全测试：用量计量汇总 API 的对外攻击面

新增对外攻击面 = 平台用量与团队 token 数据查询通道（续约对账数据属商业
敏感信息）。红→绿：

1. 越权：匿名 401、普通用户 403、API Key 通道不可达（scope 注册表
   default-deny，静态 + 运行时）；
2. 超管 200：period/totals/teams/license 结构齐、月度过滤生效、
   团队 token 聚合与 license 对账块正确；
3. 非法 month（格式/月份越界）一律 400（严格 YYYY-MM，不透传 SQL）；
4. 只读面：无写端点（GET only），不落审计白名单争议。
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import scopes
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.models.user_models import User
from app.services import jwt_service, license_service
from app.services.license_service import (
    STATUS_VALID, TIER_ENTERPRISE, LicenseState,
)
from app.api import usage_api

pytestmark = pytest.mark.unit  # W8 分类：sqlite 替身 + 进程内 license 态注入


@pytest.fixture(autouse=True)
def _no_api_key_throttle(monkeypatch):
    """隔离 API Key 认证失败节流：本机/共享 Redis 会跨测试累计失败计数
    （ip+prefix 5 次/10 分钟锁 15 分钟），多文件合跑时把 401 变成 429；
    替成 no-op 保证无效 Key 断言确定性（CI 无 Redis 本就 fail-open 不受影响）"""
    from app.services.api_key_throttle import ApiKeyAuthThrottle
    monkeypatch.setattr(ApiKeyAuthThrottle, "is_locked", lambda self, ip, prefix: False)
    monkeypatch.setattr(ApiKeyAuthThrottle, "record_failure", lambda self, ip, prefix: None)

SUMMARY = "/api/v1/admin/usage/summary"


@pytest.fixture()
def platform(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, EvaluationTask.__table__,
                                              TaskExecutionRecord.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    db = factory()
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(User(user_code="u_user", username="nobody", super_admin_flag=0, is_active=True))
    october = datetime(2026, 10, 10, tzinfo=timezone.utc)
    db.add(EvaluationTask(id="t1", name="t1", team_code="QE",
                          evaluation_type="ai_judge", status="completed", created_at=october))
    db.add(TaskExecutionRecord(id="exec_t1", task_id="t1", execution_number=1,
                               status="completed", started_at=october,
                               model_usage={"gpt-x": {"prompt_tokens": 10,
                                                      "completion_tokens": 5,
                                                      "total_tokens": 15}}))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(usage_api.router, prefix="/api/v1")
    license_service._state = LicenseState(
        status=STATUS_VALID, tier=TIER_ENTERPRISE, effective_tier=TIER_ENTERPRISE,
        customer="ACME", max_users=50, max_monthly_runs=10,
        features=["audit_export"], message="license 有效")
    yield {
        "client": TestClient(app),
        "admin_token": jwt_service.create_token("u_admin", "admin"),
        "user_token": jwt_service.create_token("u_user", "nobody"),
    }
    license_service._state = None
    db.close()


class TestUsageApiAccess:
    def test_anonymous_401(self, platform):
        assert platform["client"].get(SUMMARY).status_code == 401

    def test_non_admin_403(self, platform):
        resp = platform["client"].get(
            SUMMARY, headers={"Authorization": f"Bearer {platform['user_token']}"})
        assert resp.status_code == 403

    def test_api_key_channel_default_deny(self, platform):
        """API Key 通道：scope 注册表未登记 usage 路由 → default-deny"""
        assert scopes.match_api_key_route("GET", "/api/v1/admin/usage/summary") is None
        resp = platform["client"].get(
            SUMMARY, headers={"Authorization": "Bearer aievo_sk_invalid_token"})
        assert resp.status_code == 401  # 无效 Key 先被鉴权闸拦


class TestUsageSummary:
    def test_admin_summary_shape(self, platform):
        resp = platform["client"].get(
            f"{SUMMARY}?month=2026-10",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == "2026-10"
        assert body["totals"]["runs"] == 1
        assert body["totals"]["total_tokens"] == 15
        assert body["teams"][0]["team_code"] == "QE"
        assert body["teams"][0]["total_tokens"] == 15
        # license 对账块（续约对账依据）
        assert body["license"]["tier"] == "enterprise"
        assert body["license"]["max_monthly_runs"] == 10
        assert body["license"]["max_users"] == 50
        assert body["license"]["runs_over_quota"] is False

    def test_other_month_empty(self, platform):
        resp = platform["client"].get(
            f"{SUMMARY}?month=2026-01",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        assert resp.json()["totals"]["runs"] == 0
        assert resp.json()["teams"] == []

    def test_invalid_month_400(self, platform):
        headers = {"Authorization": f"Bearer {platform['admin_token']}"}
        for bad in ("2026-13", "2026-1", "bad", "202610", "2026/10"):
            resp = platform["client"].get(f"{SUMMARY}?month={bad}", headers=headers)
            assert resp.status_code == 400, bad
