"""
鉴权中间件
全局拦截请求，校验 Authorization: Bearer <JWT>
"""
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.jwt_service import decode_token
from app.db.session import SessionLocal
from app.models.user_models import User

logger = logging.getLogger(__name__)

# 配置中间件专用 logger，确保输出到文件
middleware_logger = logging.getLogger("app.middleware.auth_middleware")
middleware_logger.setLevel(logging.DEBUG)

# 白名单路径前缀（无需鉴权）
# 注意：不能放 "/" 作为前缀，因为它会匹配所有路径，应该精确匹配根路径
WHITELIST_PREFIXES = [
    "/health", "/docs", "/api-docs", "/openapi.json", "/assets",
    "/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/logout",
    "/api/v1/auth/status",
]


class AuthMiddleware(BaseHTTPMiddleware):
    """鉴权中间件"""

    async def dispatch(self, request: Request, call_next):
        # WebSocket 请求直接透传：BaseHTTPMiddleware 的 call_next 机制
        # 会破坏 WebSocket 握手流程，导致 422；WebSocket 鉴权在端点内部处理
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # 白名单路径直接放行（前缀匹配）
        path = request.url.path

        # 精确匹配根路径 "/"（而不是所有以 "/" 开头的路径）
        if path == "/" or any(path.startswith(prefix) for prefix in WHITELIST_PREFIXES):
            return await call_next(request)

        # 步骤1: 提取 Authorization: Bearer <token>
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            middleware_logger.warning(f"[AUTH] 401 - 未提供认证信息，请求路径: {path}")
            return JSONResponse(
                {"code": 401, "message": "未提供认证信息"},
                status_code=401
            )
        token = auth_header[7:].strip()

        # 步骤2: 校验 JWT 签名与有效期
        payload = decode_token(token)
        if not payload:
            middleware_logger.warning(f"[AUTH] 401 - token 无效或已过期，请求路径: {path}")
            return JSONResponse(
                {"code": 401, "message": "登录已失效，请重新登录"},
                status_code=401
            )

        # 步骤3: 从数据库加载用户，并在 session 关闭前提取所有属性
        with SessionLocal() as db:
            user = db.get(User, payload.get("sub"))

            if not user or not user.is_active:
                middleware_logger.warning(
                    f"[AUTH] 401 - 用户不存在或已禁用: {payload.get('sub')}，请求路径: {path}")
                return JSONResponse(
                    {"code": 401, "message": "登录已失效，请重新登录"},
                    status_code=401
                )

            user_dict = {
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
                "last_login_at": user.last_login_at,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            }

        # 将用户字典写入 request.state
        request.state.user = user_dict

        return await call_next(request)
