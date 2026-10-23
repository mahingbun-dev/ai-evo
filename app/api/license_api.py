"""License 管理 API（M6 QE6-1）：/api/v1/admin/license*

- POST /admin/license        超管上传 license 文件（multipart），验签前置——
  坏文件 400 拒绝不落库（管理员主动动作给明确反馈；与启动加载的静默降级
  口径不同：那里不能打断服务）；
- GET  /admin/license/status 超管查看当前生效态（tier/降级状态/规模与
  功能位/指纹），前端「开发者设置 → License」卡渲染依据。

安全口径（tests/security/test_license_admin_api.py 专项）：
- 两端点均超管专用（ensure_super_admin，deny by default）；
- 不进 API Key scope 注册表 → API Key 通道 default-deny（对 CI 凭证不
  开放 license 面）；
- 上传体积上限 LICENSE_MAX_BYTES（64KB），超限 413；
- 上传落审计（audited CREATE license，detail 只含客户/档位/指纹，不存原文）。
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services import license_service
from app.services.access_control import ensure_super_admin
from app.services.audit_service import audited
from app.services.license_service import LicenseError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/license", tags=["License 管理"],
                   dependencies=[Depends(ensure_super_admin)])


@router.post("", summary="上传 license 文件（超管）",
             dependencies=[Depends(audited("CREATE", "license"))])
async def upload_license(
    request: Request,
    file: UploadFile = File(..., description="厂商签发的 license 文件（JSON 信封）"),
    db: Session = Depends(get_db),
):
    """上传并立即生效：验签失败 400（不落库）；成功返回当前生效态。

    文件大小上限 LICENSE_MAX_BYTES（默认 64KB）——license 是小 JSON 信封，
    超限直接 413，防超大文件拖垮解析。
    """
    settings = get_settings()
    raw = await file.read(settings.LICENSE_MAX_BYTES + 1)
    if len(raw) > settings.LICENSE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"license 文件超过大小上限 {settings.LICENSE_MAX_BYTES} 字节（正常 license 为几 KB 内的 JSON）")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="license 文件须为 UTF-8 编码的 JSON 文件")

    try:
        state = license_service.install_license(
            db, text, uploaded_by=request.state.user.get("user_code", ""))
    except LicenseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("[LICENSE] 超管上传 license：customer=%s tier=%s status=%s",
                state.customer, state.tier, state.status)
    # QE4-4：审计 detail 只留概要（客户/档位/指纹），原文已在 platform_licenses
    request.state.audit_resource = {
        "name": state.customer or "license",
        "detail": {"customer": state.customer, "tier": state.tier,
                   "status": state.status, "fingerprint": state.fingerprint},
    }
    return state.to_view()


@router.get("/status", summary="当前 license 生效态（超管）")
def license_status():
    """当前生效态（读进程内缓存，零 DB/验签开销）。

    无 license / 过期 / 验签失败时 effective_tier 一律 oss 且带降级说明——
    核心评测不受影响，仅企业功能位锁定（方案 §M6「降级不阻断」口径）。
    """
    return license_service.current_state().to_view()
