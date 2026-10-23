"""企业功能位门面（M6 QE6-2）：统一 has(feature) 与 require_feature 闸

职责（方案 §M6 QE6-2）：
- 功能位注册表：企业功能单一事实源（key/中文标签/是否预留位），license
  的 features[] 与前端锁标渲染共用本表——license 声明未注册的 key 不生效
  （注册表之外没有「隐藏功能位」）；
- ``has(feature)``：委托 license_service（effective_tier=enterprise 且
  features 声明），预留位同口径判定（授权即 True，产品功能另期落地）；
- ``require_feature(feature)``：FastAPI 依赖工厂，未授权 403 且 detail 带
  ``{"code": "upgrade_required", ...}`` 标识——前端据此渲染升级提示而非
  普通权限错误；依赖带 ``__required_feature__`` 标记，供
  tests/security/test_entitlement_bypass.py 静态扫描企业端点旁路；
- ``status_view()``：/api/v1/auth/status 附带的 tier+功能位授权表（前端
  企业功能锁标渲染依据；免登录分享页不查本面，由 share 报告响应内嵌
  branding 判定结果）。

与既有闸的分工：require_permission（RBAC 角色）管「动不动得了」，
ensure_super_admin 管「是不是超管」，require_feature 管「买没买」——三者
正交，企业端点可叠加（如审计导出=超管 ∩ 企业位）。
"""
import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from app.services import license_service

logger = logging.getLogger(__name__)

UPGRADE_REQUIRED_CODE = "upgrade_required"


class FeatureDef:
    """功能位定义（key 唯一；reserved=预留位：授权可判、产品功能未落地）"""

    __slots__ = ("key", "label", "reserved")

    def __init__(self, key: str, label: str, reserved: bool = False):
        self.key = key
        self.label = label
        self.reserved = reserved


# 企业功能位注册表（单一事实源）：
# - audit_export        审计日志导出（M4 以 AUDIT_EXPORT_ENABLED 占位，M6 接
#                       线 license；环境位降级为紧急停止总闸，默认开）
# - report_white_label  报告白标：免登录分享报告页去 AgentEvo 品牌页脚
# - report_watermark    报告水印：免登录分享报告页去「开源版」水印
# - project_rbac        项目级细粒度 RBAC（预留——授权可判，功能未落地）
# - scim                SCIM 目录同步（预留）
# - admin_api           Admin API（预留）
FEATURE_REGISTRY: Dict[str, FeatureDef] = {
    f.key: f for f in (
        FeatureDef("audit_export", "审计导出"),
        FeatureDef("report_white_label", "报告白标"),
        FeatureDef("report_watermark", "报告水印"),
        FeatureDef("project_rbac", "项目级 RBAC", reserved=True),
        FeatureDef("scim", "SCIM 同步", reserved=True),
        FeatureDef("admin_api", "Admin API", reserved=True),
    )
}


def has(feature: str) -> bool:
    """企业功能位判定（唯一入口）：license 有效 enterprise 且声明该位。

    未注册的 key 一律 False（注册表之外无隐藏位）；license_service 侧的
    has_feature 不做注册表过滤，本门面收口后供业务面统一调用。
    """
    if feature not in FEATURE_REGISTRY:
        return False
    return license_service.has_feature(feature)


def require_feature(feature: str):
    """FastAPI 依赖工厂：企业功能位闸——未授权 403 + upgrade_required 标识。

    用法（与 require_scope/audited 同款：工厂返回裸依赖函数，调用方包
    Depends）::

        from fastapi import Depends
        @router.get("/export",
                    dependencies=[Depends(require_feature("audit_export"))])

    403 响应体 ``{"detail": {"code": "upgrade_required", "message": …,
    "feature": …}}``——「未购买」与「无权限」（RBAC 403 纯文本 detail）
    区分口径，前端据此弹升级提示。依赖函数带 ``__required_feature__``
    标记，静态断言测试据此扫描企业端点注册表（防新端点忘挂闸）。
    """
    if feature not in FEATURE_REGISTRY:
        raise ValueError(f"未注册的企业功能位 {feature!r}（先登记 FEATURE_REGISTRY）")

    def dependency(request: Request) -> None:
        if has(feature):
            return
        state = license_service.current_state()
        logger.warning(
            "[ENTITLEMENT] 403 - 企业功能位未授权（feature=%s, tier=%s, status=%s, ip=%s）",
            feature, state.effective_tier, state.status,
            request.client.host if request.client else "")
        raise HTTPException(
            status_code=403,
            detail={
                "code": UPGRADE_REQUIRED_CODE,
                "feature": feature,
                "message": (
                    f"「{FEATURE_REGISTRY[feature].label}」为企业版功能，当前部署未授权"
                    f"（license 状态：{state.status}）——请联系厂商开通后由超管上传 license"),
            },
        )

    dependency.__required_feature__ = feature
    return dependency


def status_view() -> Dict[str, Any]:
    """/auth/status 附带的授权视图：tier + 全功能位授权表（前端锁标依据）。

    每个位带 label/reserved/enabled——预留位即使授权也标注 reserved，前端
    锁标文案区分「可开通使用」与「规划中」。
    """
    state = license_service.current_state()
    return {
        "tier": state.effective_tier,
        "enterprise": state.effective_tier == license_service.TIER_ENTERPRISE,
        "features": {
            key: {
                "label": fdef.label,
                "reserved": fdef.reserved,
                "enabled": has(key),
            }
            for key, fdef in FEATURE_REGISTRY.items()
        },
    }
