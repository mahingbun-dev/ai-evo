"""M6 QE6-2 单测：entitlements 门面（has/require_feature/status_view）

覆盖口径：
- 注册表与 license_service.ENTERPRISE_FEATURES 同源一致（防两处枚举漂移）；
- has()：无 license → 全 False；enterprise+声明位 → True；未注册 key 一律
  False（注册表之外无隐藏位）；过期/验签失败降级态 → False；
- status_view()：tier/enterprise/全功能位授权表（含预留位标注）；
- require_feature：未注册位构造即 ValueError（防拼写错静默失效）。
"""
import pytest

from app.services import entitlements, license_service
from app.services.entitlements import FEATURE_REGISTRY, require_feature, status_view
from app.services.license_service import (
    STATUS_EXPIRED, STATUS_VALID, TIER_ENTERPRISE, TIER_OSS, LicenseState,
)

pytestmark = pytest.mark.unit  # W8 分类：纯内存态注入，无网络/DB


@pytest.fixture(autouse=True)
def _isolate_license_state():
    """测试间清空 license 进程内缓存（模块级单例）"""
    license_service._state = None
    yield
    license_service._state = None


def _grant(features, tier=TIER_ENTERPRISE, status=STATUS_VALID):
    """注入生效态（绕过验签，专注门面逻辑）——effective_tier 语义与
    license_service._evaluate 一致：仅 valid 且 tier=enterprise 才生效"""
    license_service._state = LicenseState(
        status=status, tier=tier, effective_tier=(
            tier if status == STATUS_VALID and tier == TIER_ENTERPRISE else TIER_OSS),
        customer="ACME", features=list(features), message="license 有效")


class TestFeatureRegistry:
    def test_registry_matches_license_service_enum(self):
        """门面注册表与 QE6-1 的 license_service 枚举一一对应（单一事实源对齐）"""
        assert set(FEATURE_REGISTRY) == set(license_service.ENTERPRISE_FEATURES)

    def test_reserved_flags(self):
        assert FEATURE_REGISTRY["project_rbac"].reserved is True
        assert FEATURE_REGISTRY["scim"].reserved is True
        assert FEATURE_REGISTRY["admin_api"].reserved is True
        assert FEATURE_REGISTRY["audit_export"].reserved is False
        assert FEATURE_REGISTRY["report_white_label"].reserved is False
        assert FEATURE_REGISTRY["report_watermark"].reserved is False


class TestHas:
    def test_no_license_all_false(self):
        for key in FEATURE_REGISTRY:
            assert entitlements.has(key) is False

    def test_enterprise_granted_feature(self):
        _grant(["audit_export", "report_watermark"])
        assert entitlements.has("audit_export") is True
        assert entitlements.has("report_watermark") is True
        assert entitlements.has("report_white_label") is False  # 未声明位

    def test_unknown_feature_always_false(self):
        _grant(["audit_export"])
        assert entitlements.has("not_a_feature") is False
        assert entitlements.has("") is False

    def test_degraded_license_denies(self):
        """过期/验签失败：effective_tier=oss → 一律 False（降级口径）"""
        _grant(["audit_export"], status=STATUS_EXPIRED)
        assert entitlements.has("audit_export") is False

    def test_oss_tier_license_denies_features(self):
        """tier=oss 的 license（即便声明 features）也不开企业位"""
        _grant(["audit_export"], tier=TIER_OSS)
        assert entitlements.has("audit_export") is False


class TestStatusView:
    def test_no_license_view(self):
        view = status_view()
        assert view["tier"] == "oss"
        assert view["enterprise"] is False
        assert set(view["features"]) == set(FEATURE_REGISTRY)
        assert all(not f["enabled"] for f in view["features"].values())
        assert view["features"]["scim"]["reserved"] is True

    def test_enterprise_view(self):
        _grant(["audit_export"])
        view = status_view()
        assert view["tier"] == "enterprise"
        assert view["enterprise"] is True
        assert view["features"]["audit_export"]["enabled"] is True
        assert view["features"]["audit_export"]["label"] == "审计导出"
        assert view["features"]["scim"]["enabled"] is False


class TestRequireFeatureFactory:
    def test_unregistered_feature_rejected(self):
        with pytest.raises(ValueError, match="未注册的企业功能位"):
            require_feature("not_a_feature")

    def test_dependency_carries_marker(self):
        dep = require_feature("audit_export")
        assert getattr(dep, "__required_feature__", None) == "audit_export"
