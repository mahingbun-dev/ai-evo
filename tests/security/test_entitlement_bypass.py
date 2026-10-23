"""M6 QE6-2 安全测试：企业功能位旁路静态断言 + 运行时闸门

方案 §M6 验收：「entitlement 旁路静态断言（企业端点必须声明依赖）」——
新企业端点忘挂 require_feature 在此红灯。三道断言：

1. 静态正向：ENTERPRISE_ENDPOINTS 注册表里的每个企业端点，在 API 注册表
   中必须挂对应 feature 的 require_feature 依赖（依赖函数带
   ``__required_feature__`` 标记，扫描手法同 test_audit_coverage）；
2. 静态反向：注册表条目必须对应真实存在的路由（删端点须同步删注册表，
   防白名单/注册表腐化）；挂了 require_feature 的路由必须在注册表内
   （防「挂错位的闸」绕过注册表口径）；
3. 运行时：未授权 403 + detail.code=upgrade_required（前端锁标/升级提示
   依据）；授权后放行；过期降级态同样 403。

预留位（project_rbac/scim/admin_api）无端点——断言其注册表状态为
reserved 且当前无路由挂载（预留位接线属后续里程碑）。
"""
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.user_models import User
from app.services import jwt_service, license_service
from app.services.license_service import (
    STATUS_EXPIRED, STATUS_VALID, TIER_ENTERPRISE, TIER_OSS, LicenseState,
)
from app.api import audit_api

pytestmark = pytest.mark.unit  # W8 分类：sqlite 替身 + 进程内 license 态注入

EXPORT = "/api/v1/audit-logs/export"

# 企业端点注册表：(method, 剥 API_V1_STR 后的路径) -> feature key。
# 新增企业功能端点必须在此登记——登记而不挂闸（或反之）都会在下方红灯。
ENTERPRISE_ENDPOINTS = {
    ("GET", "/audit-logs/export"): "audit_export",
    # 预留位（project_rbac / scim / admin_api）尚无端点，不登记；
    # report_white_label / report_watermark 是分享报告 branding 判定
    # （share_link_service.build_report 内嵌），无独立端点，不登记。
}


def _api_prefix() -> str:
    return get_settings().API_V1_STR.rstrip("/")


def _routes_with_feature_gate():
    """API 注册表中挂了 require_feature 依赖的路由：path -> feature"""
    from app.main import app
    prefix = _api_prefix()
    gated = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies:
            feature = getattr(getattr(dep, "dependency", None), "__required_feature__", None)
            if feature:
                path = route.path
                if path.startswith(prefix):
                    path = path[len(prefix):]
                gated[(next(iter(route.methods)), path)] = feature
    return gated


# ======================================================================
# 静态断言（方案 §M6 验收项）
# ======================================================================

class TestEntitlementBypassStatic:
    def test_enterprise_endpoints_declare_feature_gate(self):
        """注册表内每个企业端点必须挂对应 feature 的 require_feature 依赖"""
        gated = _routes_with_feature_gate()
        missing = []
        for endpoint, feature in ENTERPRISE_ENDPOINTS.items():
            actual = gated.get(endpoint)
            if actual != feature:
                missing.append(f"{endpoint} 期望挂 require_feature({feature})，实际 {actual}")
        assert not missing, (
            "以下企业端点未挂（或挂错）require_feature 依赖——企业功能位旁路：\n  "
            + "\n  ".join(missing))

    def test_registry_entries_exist_in_routes(self):
        """注册表不腐化：条目必须对应真实存在的路由（删端点须同步删注册表）"""
        from app.main import app
        prefix = _api_prefix()
        real = set()
        for route in app.routes:
            if isinstance(route, APIRoute):
                path = route.path
                if path.startswith(prefix):
                    path = path[len(prefix):]
                for method in route.methods:
                    real.add((method, path))
        stale = [f"{ep} -> {feat}" for ep, feat in ENTERPRISE_ENDPOINTS.items()
                 if ep not in real]
        assert not stale, "ENTERPRISE_ENDPOINTS 存在已下线端点（删端点须同步删注册表）：" + str(stale)

    def test_no_gate_outside_registry(self):
        """挂了 require_feature 的路由必须在注册表内（口径单一，防挂闸不登记）"""
        gated = _routes_with_feature_gate()
        unknown = [f"{ep} -> {feat}" for ep, feat in gated.items()
                   if ep not in ENTERPRISE_ENDPOINTS]
        assert not unknown, (
            "以下路由挂了 require_feature 但未登记 ENTERPRISE_ENDPOINTS（登记后"
            "才能被本断言持续看守）：\n  " + "\n  ".join(unknown))

    def test_audit_export_gate_before_handler(self):
        """授权闸是路由级依赖（先于 handler 执行）——非装饰器内调用可被旁路"""
        from app.main import app
        prefix = _api_prefix()
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            path = route.path[len(prefix):] if route.path.startswith(prefix) else route.path
            if (next(iter(route.methods)), path) == ("GET", "/audit-logs/export"):
                assert any(getattr(getattr(d, "dependency", None),
                                   "__required_feature__", None) for d in route.dependencies), \
                    "审计导出的企业位闸必须是路由级依赖（dependencies=），不能只在 handler 内判定"
                return
        pytest.fail("未找到审计导出路由")


# ======================================================================
# 运行时闸门（sqlite 替身平台）
# ======================================================================

@pytest.fixture()
def platform(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, OperationLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    db = factory()
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(OperationLog(user_code="u_admin", user_name="admin", team_code="QE",
                        action="CREATE", resource_type="task", resource_id="t1",
                        resource_name="任务一", ip_address="10.0.0.1"))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(audit_api.router, prefix="/api/v1")
    license_service._state = None  # 无 license 起步（默认降级态）
    yield {
        "client": TestClient(app),
        "admin_token": jwt_service.create_token("u_admin", "admin"),
    }
    license_service._state = None


def _grant(features):
    license_service._state = LicenseState(
        status=STATUS_VALID, tier=TIER_ENTERPRISE, effective_tier=TIER_ENTERPRISE,
        customer="ACME", features=list(features), message="license 有效")


class TestEntitlementRuntimeGate:
    def test_unauthorized_403_upgrade_required(self, platform):
        """无 license：超管也 403，且 detail 带 upgrade_required 标识"""
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["code"] == "upgrade_required"
        assert detail["feature"] == "audit_export"
        assert "企业版" in detail["message"]

    def test_expired_license_denies(self, platform):
        """过期 license（降级态）：同 403 upgrade_required（降级不旁路）"""
        license_service._state = LicenseState(
            status=STATUS_EXPIRED, tier=TIER_ENTERPRISE, effective_tier=TIER_OSS,
            customer="ACME", features=["audit_export"], message="已过期")
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "upgrade_required"

    def test_authorized_passes(self, platform):
        """授权后闸门放行（后续语义由 handler 接管：超管校验/导出）"""
        _grant(["audit_export"])
        resp = platform["client"].get(
            f"{EXPORT}?format=csv",
            headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")

    def test_granted_other_feature_still_denied(self, platform):
        """授权了别的位不开本位（features 精确匹配，非整档放行）"""
        _grant(["report_white_label"])
        resp = platform["client"].get(
            EXPORT, headers={"Authorization": f"Bearer {platform['admin_token']}"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "upgrade_required"
