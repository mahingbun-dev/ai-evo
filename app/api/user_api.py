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
from app.services.access_control import ensure_super_admin
from app.services.audit_service import log_operation


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
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    db: Session = Depends(get_db),
):
    """分页获取用户列表（W19：仅超管——用户目录含全员邮箱/组织等目录信息）"""
    ensure_super_admin(request)
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
async def get_user(
    request: Request,
    user_code: str,
    db: Session = Depends(get_db),
):
    """根据 user_code 获取用户详情（W19：仅超管）"""
    ensure_super_admin(request)

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
        "role": user.role,
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

    # M4 QE4-1：AUTH_METHODS 关掉 local（如纯 SSO 部署）即关闭本地注册
    if "local" not in get_settings().auth_methods:
        raise HTTPException(status_code=403, detail="本地账号登录未启用，请联系管理员使用 SSO 登录")
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
        role="engineer",
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
    """校验用户名密码，签发 JWT；此接口在白名单中，无需鉴权

    通道优先级（M4 QE4-2）：
    - 本地有密码的账号（平台自管账号）→ 本地校验，**不**旁路 LDAP；
    - 其余情况（账号不存在/无本地密码）且 AUTH_METHODS 含 ldap 且已配置
      → 旁路到 LDAP search+bind 只读认证，成功后 JIT 开户/映射并签发；
    - 两条通道都不可用/都失败 → 401（文案与历史一致，不区分存在性）。
    AUTH_METHODS 关掉 local（纯 SSO/LDAP 部署口径）时本地直登整体关闭。
    """
    from app.core.config import get_settings

    methods = get_settings().auth_methods
    if "local" not in methods and "ldap" not in methods:
        raise HTTPException(status_code=403, detail="本地账号登录未启用，请联系管理员使用 SSO 登录")

    user = db.query(User).filter(User.username == body.username).first()
    # 本地有密码的账号走本地校验（平台自管，不旁路——LDAP 侧同名账号
    # 不构成对本地账号的密码证明）
    if "local" in methods and user is not None and user.password_hash:
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
    elif "ldap" in methods:
        user = _ldap_login_fallback(db, body)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
    else:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    user.last_login_at = datetime.utcnow()
    db.commit()

    logger.info(f"[AUTH] 用户登录: {user.username}")
    return {"token": create_token(user.user_code, user.username), "user": _user_brief(user)}


def _ldap_login_fallback(db: Session, body: "LoginRequest"):
    """LDAP search+bind 旁路（M4 QE4-2）：成功返回已 JIT 的用户，失败 None

    - 目录交互（authenticate）的失败一律不外显：LdapError 与意外异常
      （连接层抛错等）统一按 None（401「用户名或密码错误」），不向未认证方
      暴露目录状态、不带病 500，详细原因进日志由管理员排障；
    - 唯一例外是映射到本地已禁用账号（SsoError）：与本地登录同口径 403。
    """
    from app.services import ldap_service, sso_service
    from app.services.sso_service import SsoError

    config = ldap_service.get_active_config(db)
    if config is None:
        return None
    try:
        claims = ldap_service.authenticate(config, body.username, body.password)
    except ldap_service.LdapError as e:
        logger.warning(f"[AUTH] LDAP 旁路失败: {e}")
        return None
    except Exception as e:  # 目录层意外异常（连接抛错等）：失败关闭
        logger.warning(f"[AUTH] LDAP 旁路异常: {e.__class__.__name__}: {e}", exc_info=True)
        return None
    try:
        user, created = sso_service.provision_user(db, claims, source="ldap")
    except SsoError as e:
        # 映射到本地禁用账号等：语义性拒绝，403 与本地通道一致
        raise HTTPException(status_code=403, detail=str(e))
    log_operation(
        {"user_code": user.user_code, "user_name": user.user_name,
         "org_code": user.org_code},
        action="CREATE" if created else "LOGIN",
        resource_type="user" if created else "ldap",
        resource_id=user.user_code,
        resource_name=user.username,
        detail={"via": "ldap", "email": claims["email"],
                "groups": claims["groups"]},
    )
    return user


@auth_router.get("/status", summary="检查登录状态")
async def check_auth_status(request: Request):
    """检查当前登录状态（此接口不拦截，自查 Bearer token；W13 如实化）

    未登录/凭证无效时返回 401 并带 message——调用方（前端路由守卫、部署冒烟）
    才能据状态码区分「已登录」与「登录态不可用」。is_authenticated 字段保持向后兼容。

    M6 QE6-2：已登录响应附带 license 块（tier + 企业功能位授权表）——前端
    据此渲染企业功能锁标（未登录分支不带：无登录态锁标无渲染场景）。
    """
    from app.services import entitlements
    user_dict = _auth_user_dict(request)
    if not user_dict:
        return JSONResponse(
            content={"is_authenticated": False, "user": None, "message": "未提供认证信息或登录已失效"},
            status_code=401
        )
    return {"is_authenticated": True, "user": user_dict, "license": entitlements.status_view()}


@auth_router.post("/logout", summary="退出登录")
async def logout():
    """本地 JWT 无服务端会话，登出由前端清除本地 token 完成"""
    return {"success": True}
