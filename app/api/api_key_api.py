"""
API Key 管理 API（M1 QE1-4）：/api/v1/api-keys 创建/列表/吊销

- **登录态专用**：本组端点不进 API Key 路由注册表（scopes default-deny），
  API Key 不能管理 API Key——凭证自管理是账号主人的浏览器会话特权；
- 明文 token 仅创建时返回一次（与 webhook secret 同口径），列表/查询
  只回 key_prefix；
- 吊销 = revoked_at 置时间（软删，审计留痕），不物理删除；
- 列表口径：本人创建的 Key（user_code）；吊销同理（或超管代管）。
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import scopes as scope_constants
from app.db.session import get_db
from app.models.api_key_models import ApiKey
from app.models.user_models import User
from app.services import api_key_service
from app.services.access_control import is_super_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


# ==================== Request / Response Models ====================

class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Key 名称（展示用）")
    scopes: List[str] = Field(..., description=f"授权 scope，可选：{list(scope_constants.ALL_SCOPES)}")
    expires_at: Optional[datetime] = Field(None, description="过期时间（空=永不过期）")


class ApiKeyResponse(BaseModel):
    """Key 视图（不含 token——明文仅创建时返回一次）"""
    id: int
    user_code: str
    team_code: Optional[str] = None
    name: str
    key_prefix: str
    scopes: Optional[List[str]] = None
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApiKeyCreateResponse(ApiKeyResponse):
    """创建响应：多一个 token 明文（仅此一次）"""
    token: str = Field(..., description="aievo_sk_... 完整 token，仅创建时返回一次，请妥善保存")


# ==================== 端点 ====================

def _current_user_code(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    user_code = user.get("user_code")
    if not user_code:
        raise HTTPException(status_code=401, detail="未认证")
    return user_code


def _own_key_or_admin(db: Session, request: Request, key_id: int) -> ApiKey:
    """查出 + 归属校验：本人创建的 Key 或超管可操作，否则 404/403"""
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    if key.user_code != _current_user_code(request) and not is_super_admin(request.state.user):
        # 归属不符按 404 口径（不泄露他人 Key 的存在性）
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return key


@router.post("", response_model=ApiKeyCreateResponse, summary="创建 API Key")
def create_api_key(body: ApiKeyCreateRequest, request: Request, db: Session = Depends(get_db)):
    """创建 API Key（登录态）。完整 token 仅本次响应明文返回，库内只存
    sha256——丢失只能吊销重建。scope 须为白名单子集。"""
    from app.services.access_control import user_team_code

    user_code = _current_user_code(request)
    if body.expires_at is not None:
        # 时区兜底：naive 视为 UTC（OpenAPI 传 ISO 串多为 aware，此为保险）
        expires_at = body.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="过期时间必须晚于当前时间")
    else:
        expires_at = None
    try:
        record, token = api_key_service.create_api_key(
            db,
            user_code=user_code,
            name=body.name,
            granted_scopes=body.scopes,
            team_code=user_team_code(request.state.user),
            expires_at=expires_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"[APIKEY] 创建 API Key: id={record.id} user={user_code} scopes={record.scopes}")
    resp = ApiKeyCreateResponse(**ApiKeyResponse.model_validate(record).model_dump(), token=token)
    return resp


@router.get("", response_model=List[ApiKeyResponse], summary="列出我的 API Key")
def list_api_keys(request: Request, db: Session = Depends(get_db)):
    """列出当前登录用户创建的 API Key（含已吊销，便于审计）；不含 token 明文。

    超管同口径只看自己的 Key（Key 是个人凭证，不按团队共享）。
    """
    user_code = _current_user_code(request)
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_code == user_code)
        .order_by(ApiKey.id.desc())
        .all()
    )


@router.delete("/{key_id}", response_model=ApiKeyResponse, summary="吊销 API Key")
def revoke_api_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    """吊销 API Key（软删：revoked_at 置时间，幂等；历史留痕不物理删除）"""
    key = _own_key_or_admin(db, request, key_id)
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(key)
        logger.info(f"[APIKEY] 吊销 API Key: id={key.id} user={key.user_code}")
    return key
