"""
用户管理 API
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Query

logger = logging.getLogger(__name__)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from datetime import datetime

from app.db.session import get_db, SessionLocal
from app.models.user_models import User
from app.services.jwt_service import create_token, decode_token
from app.services.password_service import hash_password, verify_password


# ==================== Response Models ====================

class UserResponse(BaseModel):
    """用户响应模型"""
    model_config = ConfigDict(from_attributes=True)

    user_code: str
    username: Optional[str] = None
    user_name: Optional[str] = None
    email: Optional[str] = None
    employee_id: Optional[str] = None
    org_code: Optional[str] = None
    org_name: Optional[str] = None
    position_name: Optional[str] = None
    user_type: Optional[int] = None
    super_admin_flag: Optional[int] = None
    application_admin: Optional[int] = None
    is_active: bool = True
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_serializer('last_login_at')
    def serialize_last_login_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    @field_serializer('created_at')
    def serialize_created_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    @field_serializer('updated_at')
    def serialize_updated_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class UserListResponse(BaseModel):
    """用户列表响应"""
    total: int
    items: List[UserResponse]


# ==================== Routers ====================

router = APIRouter(prefix="/users", tags=["用户管理"])
auth_router = APIRouter(prefix="/auth", tags=["用户认证"])


# ==================== User Endpoints ====================

@router.get("/me", response_model=UserResponse, summary="获取当前用户信息")
async def get_current_user(request: Request):
    """获取当前登录用户信息"""
    user = request.state.user
    return user


@router.get("/", response_model=UserListResponse, summary="获取用户列表")
async def list_users(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    db: Session = Depends(get_db),
):
    """分页获取用户列表"""
    query = db.query(User)

    if keyword:
        query = query.filter(
            or_(
                User.user_name.ilike(f"%{keyword}%"),
                User.user_code.ilike(f"%{keyword}%"),
                User.email.ilike(f"%{keyword}%")
            )
        )

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return UserListResponse(total=total, items=items)


@router.get("/{user_code}", response_model=UserResponse, summary="获取指定用户信息")
async def get_user(user_code: str, request: Request, db: Session = Depends(get_db)):
    """根据 user_code 获取用户详情（仅本人或超级管理员：响应含邮箱等联系方式）"""
    caller = getattr(request.state, "user", None) or {}
    if caller.get("super_admin_flag") != 1 and caller.get("user_code") != user_code:
        raise HTTPException(status_code=403, detail="仅可查看本人信息（管理员除外）")
    user = db.get(User, user_code)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


# ==================== Auth Endpoints ====================


def _user_brief(user: User) -> dict:
    """用户公开信息（不含密码哈希）"""
    return {
        "user_code": user.user_code,
        "username": user.username,
        "user_name": user.user_name,
        "email": user.email,
        "employee_id": user.employee_id,
        "org_code": user.org_code,
        "org_name": user.org_name,
        "position_name": user.position_name,
        "user_type": user.user_type,
        "super_admin_flag": user.super_admin_flag,
        "application_admin": user.application_admin,
        "is_active": user.is_active,
    }


def _auth_user_dict(request: Request) -> Optional[dict]:
    """从 Authorization: Bearer <JWT> 解析当前用户（白名单接口自查用）"""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    payload = decode_token(auth_header[7:].strip())
    if not payload:
        return None
    with SessionLocal() as db:
        user = db.get(User, payload.get("sub"))
        if not user or not user.is_active:
            return None
        return _user_brief(user)


class RegisterRequest(BaseModel):
    """注册请求"""
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$", description="用户名（字母/数字/_.-）")
    password: str = Field(..., min_length=6, max_length=128, description="密码（至少 6 位）")
    user_name: Optional[str] = Field(None, max_length=64, description="显示名，缺省用用户名")
    email: Optional[str] = Field(None, max_length=128, description="邮箱（可选）")


class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


@auth_router.post("/register", summary="注册账号")
async def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """
    开放注册：系统首个注册用户自动获得超级管理员权限
    此接口在白名单中，无需鉴权
    """
    import uuid
    from app.core.config import get_settings

    if not get_settings().AUTH_REGISTER_ENABLED:
        raise HTTPException(status_code=403, detail="当前未开放注册")

    exists = db.query(User.user_code).filter(User.username == body.username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    is_first = db.query(User.user_code).first() is None
    user = User(
        user_code=f"u_{uuid.uuid4().hex[:16]}",
        username=body.username,
        password_hash=hash_password(body.password),
        user_name=body.user_name or body.username,
        email=body.email or "",
        super_admin_flag=1 if is_first else 0,
        application_admin=0,
        is_active=True,
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info(f"[AUTH] 新用户注册: {user.username} (首个用户: {is_first})")
    return {"token": create_token(user.user_code, user.username), "user": _user_brief(user)}


@auth_router.post("/login", summary="账号密码登录")
async def login(body: LoginRequest, db: Session = Depends(get_db)):
    """校验用户名密码，签发 JWT；此接口在白名单中，无需鉴权"""
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    user.last_login_at = datetime.utcnow()
    db.commit()

    logger.info(f"[AUTH] 用户登录: {user.username}")
    return {"token": create_token(user.user_code, user.username), "user": _user_brief(user)}


@auth_router.get("/status", summary="检查登录状态")
async def check_auth_status(request: Request):
    """检查当前登录状态（此接口不拦截，自查 Bearer token）"""
    user_dict = _auth_user_dict(request)
    if not user_dict:
        return JSONResponse(
            content={"is_authenticated": False, "user": None},
            status_code=200
        )
    return {"is_authenticated": True, "user": user_dict}


@auth_router.post("/logout", summary="退出登录")
async def logout():
    """本地 JWT 无服务端会话，登出由前端清除本地 token 完成"""
    return {"success": True}
