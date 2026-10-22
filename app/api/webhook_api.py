"""
Webhook API（M1 QE1-3）：/api/v1/webhooks 端点 CRUD 与送达历史

- 登录态（页面）与 API Key（webhooks:manage scope）双通道；scope 路由
  注册表 + require_scope 纵深防御（与 QE1-2 gate 端点同机制）；
- secret 仅创建时明文返回一次（与 API Key 同口径），列表/查询不回显；
- 团队隔离：端点按 team_code 归属（user_team_code 口径），跨团队 403。
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy.orm import Session

from app.core import scopes
from app.core.scopes import require_scope
from app.db.session import get_db
from app.services import webhook_service
from app.services.access_control import (
    AccessDeniedError,
    check_team_access,
    resource_team_code,
    user_team_code,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhook"])

# M1 QE1-3：webhook 管理端点进 API Key 路由注册表（webhooks:manage）
scopes.register_api_key_route("POST", "/api/v1/webhooks", scopes.WEBHOOKS_MANAGE)
scopes.register_api_key_route("GET", "/api/v1/webhooks", scopes.WEBHOOKS_MANAGE)
scopes.register_api_key_route(
    "DELETE", "/api/v1/webhooks/{webhook_id}", scopes.WEBHOOKS_MANAGE)
scopes.register_api_key_route(
    "GET", "/api/v1/webhooks/{webhook_id}/deliveries", scopes.WEBHOOKS_MANAGE)


# ==================== Request / Response Models ====================

class WebhookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="端点名称")
    url: str = Field(..., min_length=1, max_length=500, description="接收端 URL（http/https）")
    events: List[str] = Field(..., description=f"订阅事件，可选：{list(webhook_service.WEBHOOK_EVENTS)}")


class WebhookResponse(BaseModel):
    """端点视图（不含 secret——明文仅创建时返回一次）"""
    id: int
    team_code: str
    name: str
    url: str
    events: List[str]
    active: bool
    created_by: Optional[str] = None

    class Config:
        from_attributes = True


class WebhookCreateResponse(WebhookResponse):
    """创建响应：多一个 secret 明文（仅此一次）"""
    secret: str = Field(..., description="签名 secret（whsec_ 前缀，仅创建时返回一次，请妥善保存）")


class DeliveryResponse(BaseModel):
    id: int
    endpoint_id: int
    event: str
    payload: Optional[Dict[str, Any]] = None
    status: str
    response_status: Optional[int] = None
    attempts: int
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

    @field_serializer("created_at")
    def serialize_created_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


# ==================== 端点 ====================

def _ensure_endpoint_access(db: Session, user, endpoint_id: int):
    """查出 + 团队归属校验一步完成（W9 ensure_* 口径）"""
    endpoint = db.get(webhook_service.WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="webhook 端点不存在")
    try:
        check_team_access(user, resource_team_code(endpoint.team_code))
    except AccessDeniedError:
        raise HTTPException(status_code=403, detail="无权访问该 webhook（跨团队操作被拒绝）")
    return endpoint


@router.post(
    "",
    response_model=WebhookCreateResponse,
    summary="创建 webhook 端点",
    dependencies=[Depends(require_scope(scopes.WEBHOOKS_MANAGE))],
)
def create_webhook(body: WebhookCreateRequest, request: Request, db: Session = Depends(get_db)):
    """创建团队级 webhook 端点。secret 仅本次响应明文返回，库内不再可查。

    URL 经 SSRF 守卫校验（默认拒绝 localhost/私网/链路本地；企业内网接收端
    在 .env 设 WEBHOOK_ALLOW_PRIVATE_TARGETS=true 放行）。
    """
    try:
        endpoint, secret = webhook_service.create_endpoint(
            db,
            team_code=user_team_code(request.state.user),
            name=body.name,
            url=body.url,
            events=body.events,
            created_by=(request.state.user or {}).get("user_code"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    resp = WebhookCreateResponse.model_validate(endpoint)
    resp.secret = secret
    return resp


@router.get(
    "",
    response_model=List[WebhookResponse],
    summary="列出本团队 webhook 端点",
    dependencies=[Depends(require_scope(scopes.WEBHOOKS_MANAGE))],
)
def list_webhooks(request: Request, db: Session = Depends(get_db)):
    """列出端点（普通用户=本团队；超管=全部）。响应不含 secret。"""
    from app.services.access_control import filter_team_code

    return webhook_service.list_endpoints(db, filter_team_code(request.state.user))


@router.delete(
    "/{webhook_id}",
    response_model=WebhookResponse,
    summary="删除 webhook 端点",
    dependencies=[Depends(require_scope(scopes.WEBHOOKS_MANAGE))],
)
def delete_webhook(webhook_id: int, request: Request, db: Session = Depends(get_db)):
    """删除端点（物理删；投递留痕级联清理）"""
    endpoint = _ensure_endpoint_access(db, request.state.user, webhook_id)
    webhook_service.delete_endpoint(db, webhook_id)
    return endpoint


@router.get(
    "/{webhook_id}/deliveries",
    response_model=List[DeliveryResponse],
    summary="查询端点的投递历史",
    dependencies=[Depends(require_scope(scopes.WEBHOOKS_MANAGE))],
)
def list_webhook_deliveries(
    webhook_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """送达历史留痕（倒序：最新在前；status/attempts/response_status/last_error）"""
    _ensure_endpoint_access(db, request.state.user, webhook_id)
    return webhook_service.list_deliveries(db, webhook_id, limit=limit)
