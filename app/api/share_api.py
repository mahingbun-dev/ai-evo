"""分享链接 API（M5 QE5-4，报告外发）

两组路由：

- ``manage_router``（鉴权面，挂 ``/api/v1/evaluation``）：创建/列表/吊销，
  走 ensure_permission + ensure_task_access（viewer 只读 403、跨团队 403），
  创建/吊销落审计；
- ``public_router``（免鉴权，挂 ``/api/v1/share``）：``GET /share/{token}/report``
  免登录只读报告——中间件白名单放行（见 auth_middleware WHITELIST_PREFIXES），
  端点内 Redis IP 限流（10 req/min）+ 无差别 404（不存在/吊销/过期同码），
  成功访问落 VIEW 审计（匿名，user_code 空）。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.access_control import ensure_permission, ensure_task_access
from app.services.audit_service import audited, log_operation
from app.services.share_link_service import (
    ShareLinkService,
    check_share_rate_limit,
)

logger = logging.getLogger(__name__)

# 鉴权面：任务子资源（/evaluation/tasks/{task_id}/share-links）
manage_router = APIRouter(prefix="/evaluation", tags=["分享链接"])

# 免鉴权面：/share/{token}/report（白名单 + 端点内限流）
public_router = APIRouter(prefix="/share", tags=["分享链接"])


class CreateShareLinkRequest(BaseModel):
    """创建分享链接：有效期/范围可选，锁定执行次数可选（空=跟随最新）"""
    execution_number: Optional[int] = Field(None, description="锁定执行次数（空=跟随最新执行）")
    expires_days: Optional[int] = Field(None, description="有效期（天）：1/7/30，空=永久")
    row_scope: str = Field("all", description="可见范围：all-全部明细 / summary-仅汇总")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@manage_router.post(
    "/tasks/{task_id}/share-links",
    summary="创建报告分享链接",
    dependencies=[Depends(audited("CREATE", "share_link"))],
)
def create_share_link(
    task_id: str,
    body: CreateShareLinkRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """创建免登录只读报告分享链接（明文 token 仅本次响应返回一次）"""
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 403
    task = ensure_task_access(db, request.state.user, task_id)
    service = ShareLinkService(db)
    user = request.state.user
    user_code = user.get("user_code") if isinstance(user, dict) else getattr(user, "user_code", None)
    try:
        link, token = service.create_link(
            task.id,
            execution_number=body.execution_number,
            expires_days=body.expires_days,
            row_scope=body.row_scope,
            user_code=user_code,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 创建类端点无路径资源 ID：暂存新链接供审计留痕
    request.state.audit_resource = {
        "id": link.id, "name": task.name,
        "detail": {"task_id": task.id, "row_scope": link.row_scope,
                   "execution_number": link.execution_number},
    }
    return {"token": token, "link": service.link_payload(link)}


@manage_router.get("/tasks/{task_id}/share-links", summary="任务的分享链接列表")
def list_share_links(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """任务的分享链接列表（含有效状态推导；明文 token 不再可见）"""
    task = ensure_task_access(db, request.state.user, task_id)
    return ShareLinkService(db).list_links(task.id)


@manage_router.delete(
    "/tasks/{task_id}/share-links/{link_id}",
    summary="吊销分享链接",
    dependencies=[Depends(audited("DELETE", "share_link", id_param="link_id"))],
)
def revoke_share_link(
    task_id: str,
    link_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """吊销分享链接（立即失效；幂等——已吊销再吊仍 200）"""
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 403
    ensure_task_access(db, request.state.user, task_id)
    link = ShareLinkService(db).revoke_link(task_id, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="分享链接不存在")
    return ShareLinkService(db).link_payload(link)


@public_router.get("/{token}/report", summary="免登录只读报告（分享链接）")
def get_shared_report(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """免登录只读报告（M5 QE5-4）

    安全口径（方案 §5.4）：Redis 限流 10 req/min/IP（超限 429）；token
    不存在/已吊销/已过期一律 404 且响应体不区分原因（枚举防护）；成功
    访问落 VIEW 审计（匿名访问者，user_code 空，记 IP）。
    """
    from app.services.redis_stop_signal import get_redis_client

    if not check_share_rate_limit(get_redis_client(), _client_ip(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    link = ShareLinkService(db).get_link_by_token(token)
    if link is None:
        # 无差别 404：不区分不存在/吊销/过期
        raise HTTPException(status_code=404, detail="报告不存在或已失效")

    report = ShareLinkService(db).build_report(link)

    # 访问落审计（旁路：审计失败不影响报告返回）
    try:
        log_operation(
            user={},
            action="VIEW",
            resource_type="share_link",
            resource_id=link.id,
            detail={"method": request.method, "path": request.url.path,
                    "task_id": link.task_id, "row_scope": link.row_scope},
            ip_address=_client_ip(request),
        )
    except Exception as e:  # noqa: BLE001 - 审计旁路
        logger.warning(f"分享链接访问审计落库失败（不影响报告返回）: {e}")
    return report
