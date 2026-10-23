"""M4 QE4-4 → M6 QE6-2 安全测试：审计导出（xlsx/csv）企业功能位闸门

新增对外攻击面 = 审计整表数据外发通道（tests/security 纪律配套）。红→绿：

1. 授权闸（M6 QE6-2 起=license 功能位 audit_export）：无 license 超管访问
   403 + detail.code=upgrade_required（与权限不足 403 区分口径）；
2. 越权：匿名 401、普通用户 403、API Key 通道不可达（scope 注册表
   default-deny，未登记路由对 API Key 全量 403）；
3. 授权后超管可导出：csv（utf-8-sig BOM，Excel 直开不乱码）/ xlsx
   （openpyxl 可解析，行数=日志数+表头）；过滤条件生效；
4. 单次行数上限：超限 400 且不产出文件（防整表拖库打爆内存）；
5. 非法 format 一律 400（白名单 csv/xlsx，不透传其他序列化器）；
6. 紧急停止总闸 AUDIT_EXPORT_ENABLED=false（默认 true）：授权方也 404
   （能力被管理员关闭，与未购买 403 区分）。
"""
import io
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import scopes
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.user_models import User
from app.services import jwt_service, license_service
from app.services.license_service import (
    STATUS_VALID, TIER_ENTERPRISE, LicenseState,
)
from app.api import audit_api

pytestmark = pytest.mark.unit  # W8 分类：sqlite 替身 + 进程内 license 态注入


@pytest.fixture(autouse=True)
def _no_api_key_throttle(monkeypatch):
    """隔离 API Key 认证失败节流：本机/共享 Redis 会跨测试累计失败计数
    （ip+prefix 5 次/10 分钟锁 15 分钟），多文件合跑时把 401 变成 429；
    替成 no-op 保证无效 Key 断言确定性（CI 无 Redis 本就 fail-open 不受影响）"""
    from app.services.api_key_throttle import ApiKeyAuthThrottle
    monkeypatch.setattr(ApiKeyAuthThrottle, "is_locked", lambda self, ip, prefix: False)
    monkeypatch.setattr(ApiKeyAuthThrottle, "record_failure", lambda self, ip, prefix: None)

EXPORT = "/api/v1/audit-logs/export"


@pytest.fixture()
def platform(monkeypatch):
    """挂载审计路由 + AuthMiddleware 的最小平台（sqlite 替身）"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, OperationLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    db = factory()
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(User(user_code="u_user", username="nobody", super_admin_flag=0, is_active=True))
    db.add(OperationLog(user_code="u_admin", user_name="admin", team_code="QE",
                        action="CREATE", resource_type="task", resource_id="t1",
                        resource_name="任务一", ip_address="10.0.0.1"))
    db.add(OperationLog(user_code="u_user", user_name="nobody", team_code="QE",
                        action="UPDATE", resource_type="dataset", resource_id="d1",
                        resource_name="数据集一", ip_address="10.0.0.2"))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(audit_api.router, prefix="/api/v1")
    license_service._state = None  # 无 license 起步（默认降级态）
    return {
        "client": TestClient(app),
        "db": db,
        "factory": factory,
        "admin_token": jwt_service.create_token("u_admin", "admin"),
        "user_token": jwt_service.create_token("u_user", "nobody"),
    }


@pytest.fixture(autouse=True)
def _isolate_license_state():
    """测试间清空 license 进程内缓存（模块级单例）"""
    license_service._state = None
    yield
    license_service._state = None


@pytest.fixture()
def audit_export_granted():
    """注入授权态：enterprise license 声明 audit_export 位"""
    license_service._state = LicenseState(
        status=STATUS_VALID, tier=TIER_ENTERPRISE, effective_tier=TIER_ENTERPRISE,
        customer="ACME", features=["audit_export"], message="license 有效")


class TestAuditExportGate:
    def test_unauthorized_403_upgrade_required(self, platform):
        """无 license：超管 403 + upgrade_required 标识（M6 QE6-2 新口径）"""
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == "upgrade_required"
        assert detail["feature"] == "audit_export"

    def test_anonymous_401(self, platform, audit_export_granted):
        assert platform["client"].get(EXPORT).status_code == 401

    def test_non_admin_403(self, platform, audit_export_granted):
        """授权已过闸：普通用户被 handler 的超管校验拦（纯文本 detail）"""
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['user_token']}"})
        assert resp.status_code == 403
        assert "超级管理员" in resp.json()["detail"]

    def test_api_key_channel_denied(self, platform, audit_export_granted):
        """API Key 通道：scope 注册表未登记审计路由 → default-deny 403（静态 + 运行时）"""
        assert scopes.match_api_key_route("GET", "/api/v1/audit-logs/export") is None
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": "Bearer aievo_sk_invalid_token"})
        assert resp.status_code == 401  # 无效 Key 先被鉴权闸拦（401），合法 Key 走注册表 403

    def test_emergency_stop_404_when_disabled(self, platform, audit_export_granted, monkeypatch):
        """紧急停止总闸 AUDIT_EXPORT_ENABLED=false：授权方也 404（能力不存在）"""
        from app.core.config import get_settings
        monkeypatch.setattr(get_settings(), "AUDIT_EXPORT_ENABLED", False)
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 404
        assert "AUDIT_EXPORT_ENABLED" in resp.json()["detail"]

    def test_admin_csv_export(self, platform, audit_export_granted):
        resp = platform["client"].get(
            f"{EXPORT}?format=csv",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers.get("content-disposition", "")
        text = resp.content.decode("utf-8-sig")  # BOM 剥离后可读（Excel 直开口径）
        assert "user_code" in text and "CREATE" in text and "任务一" in text

    def test_admin_xlsx_export(self, platform, audit_export_granted):
        from openpyxl import load_workbook

        resp = platform["client"].get(
            f"{EXPORT}?format=xlsx",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        sheet = load_workbook(io.BytesIO(resp.content)).active
        rows = list(sheet.iter_rows(values_only=True))
        assert rows[0][0] == "id"  # 表头
        assert len(rows) == 3  # 表头 + 2 条日志

    def test_filter_narrows_export(self, platform, audit_export_granted):
        resp = platform["client"].get(
            f"{EXPORT}?format=csv&action=UPDATE",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        text = resp.content.decode("utf-8-sig")
        assert "UPDATE" in text and "CREATE" not in text

    def test_row_cap_400(self, platform, audit_export_granted, monkeypatch):
        from app.core.config import get_settings
        monkeypatch.setattr(get_settings(), "AUDIT_EXPORT_MAX_ROWS", 1)
        resp = platform["client"].get(
            f"{EXPORT}?format=csv",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 400
        assert "收窄" in resp.json()["detail"]

    def test_invalid_format_400(self, platform, audit_export_granted):
        resp = platform["client"].get(
            f"{EXPORT}?format=pdf",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 400
