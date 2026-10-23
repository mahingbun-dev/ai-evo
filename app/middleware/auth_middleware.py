"""
鉴权中间件
全局拦截请求，校验 Authorization: Bearer <JWT 或 API Key>

M1 QE1-1：Bearer token 以 ``aievo_sk_`` 开头走 API Key 分支（CI/CLI 长期凭证，
app/services/api_key_service.py）——sha256 比对 + 有效期/吊销/用户活跃校验 +
认证失败节流（app/services/api_key_throttle.py）+ scope 路由注册表
default-deny（app/core/scopes.py）；其余 token 走既有 JWT 分支，行为不变。
"""
import logging
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.jwt_service import decode_token
from app.db.session import SessionLocal
from app.models.user_models import User
from app.services.api_key_service import API_KEY_TOKEN_PREFIX, parse_api_key, verify_api_key
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.core import scopes as scope_constants

logger = logging.getLogger(__name__)

# 配置中间件专用 logger，确保输出到文件
# （W13：DEBUG 降 INFO，与 main.py 的统一日志配置对齐，避免每请求刷 SSO 明细）
middleware_logger = logging.getLogger("app.middleware.auth_middleware")
middleware_logger.setLevel(logging.INFO)

# 白名单路径前缀（无需鉴权）
# 注意：不能放 "/" 作为前缀，因为它会匹配所有路径，应该精确匹配根路径
# W13：/api/v1/health（深健康检查）入白名单——探活请求不带业务凭证，
# build-service 部署校验与 compose healthcheck 都依赖匿名可达
# M4 QE4-1：OIDC SSO 匿名三端点——methods（登录页渲染依据）/login
# （302 到 IdP）/callback（IdP 回跳换 token）。注意 /auth/oidc/config、
# /auth/oidc/test 等超管配置面**不在**白名单（前缀精确到叶子路径）
# M5 QE5-4：/api/v1/share/{token}/report 免登录只读报告——凭证即 token
# 本身（sha256 比对 + 吊销/过期无差别 404），端点内另有 Redis IP 限流
# （10 req/min）与访问审计；share 域没有其他匿名子路径
WHITELIST_PREFIXES = [
    "/health", "/docs", "/api-docs", "/openapi.json", "/assets",
    "/api/v1/health",
    "/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/logout",
    "/api/v1/auth/status", "/api/v1/auth/methods",
    "/api/v1/auth/oidc/login", "/api/v1/auth/oidc/callback",
    "/api/v1/share/",
]


def _build_user_dict(user: User) -> dict:
    """User ORM 行 → request.state.user 字典（session 关闭前提取全部属性）"""
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
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _client_ip(request: Request) -> str:
    """直连对端 IP（节流计数口径）。

    不采信 X-Forwarded-For：该头可伪造，采信等于让攻击者每次请求换 IP
    绕过计数；反代部署下按直连地址（反代 IP）计数，全员共享额度偏保守，
    与 fail-open 语义同为「宁松勿断」的护栏取向。
    """
    return request.client.host if request.client else "unknown"


class AuthMiddleware(BaseHTTPMiddleware):
    """鉴权中间件"""

    def __init__(self, app, api_key_throttle: ApiKeyAuthThrottle = None):
        super().__init__(app)
        # 节流器可注入（tests/security 传假 Redis）；缺省接全局 Redis 连接口径
        self._api_key_throttle = api_key_throttle or ApiKeyAuthThrottle()

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

        # 非 API 路径放行：生产模式前端构建产物由本服务托管（SPA catch-all 返回
        # index.html），前端路由（/login、/tasks 等）必须可直达，登录态由前端
        # 路由守卫控制；接口数据仍全部走下方 token 校验
        if not path.startswith("/api/"):
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

        # 步骤1.5（M1 QE1-1）：aievo_sk_ 前缀走 API Key 分支，之后逻辑与
        # JWT 分支完全隔离（JWT 路径行为不变）
        if token.startswith(API_KEY_TOKEN_PREFIX):
            return await self._authenticate_api_key(request, token, call_next)

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

            if not user:
                middleware_logger.warning(
                    f"[AUTH] 401 - 用户不存在: {payload.get('sub')}，请求路径: {path}")
                return JSONResponse(
                    {"code": 401, "message": "登录已失效，请重新登录"},
                    status_code=401
                )

            # W19：本地禁用的账号直接拒绝（403 与未认证 401 区分，
            # 否则团队管理页的「禁用成员」对 API 访问完全无效）
            if not user.is_active:
                middleware_logger.warning(
                    f"[AUTH] 403 - 账号已被禁用，请求路径: {path}, user_code={user.user_code}")
                return JSONResponse(
                    {"code": 403, "message": "账号已被禁用，请联系管理员"},
                    status_code=403
                )

            user_dict = _build_user_dict(user)

        # 将用户字典写入 request.state
        request.state.user = user_dict
        # JWT 登录态：无 scope 概念（require_scope 依赖据此放行）
        request.state.auth_type = "jwt"
        request.state.api_key_scopes = None

        return await call_next(request)

    async def _authenticate_api_key(self, request: Request, token: str, call_next):
        """API Key 分支（M1 QE1-1）：节流 → sha256 认证 → 用户活跃 → scope 路由闸

        统一失败文案「API Key 无效或已失效」（不区分 hash 错/吊销/过期，
        防探测）；内部 reason 只进日志。
        """
        path = request.url.path
        ip = _client_ip(request)
        throttle = self._api_key_throttle

        # 节流前置：锁定期内不查库不比对，直接 429
        prefix = parse_api_key(token) or "malformed"
        if throttle.is_locked(ip, prefix):
            middleware_logger.warning(
                f"[AUTH] 429 - API Key 认证失败锁定中，路径: {path}, ip: {ip}")
            return JSONResponse(
                {"code": 429, "message": "认证失败次数过多，请稍后再试"},
                status_code=429
            )

        with SessionLocal() as db:
            record, reason = verify_api_key(db, token)
            if record is None:
                throttle.record_failure(ip, prefix)
                middleware_logger.warning(
                    f"[AUTH] 401 - API Key 无效（{reason}），路径: {path}, ip: {ip}")
                return JSONResponse(
                    {"code": 401, "message": "API Key 无效或已失效"},
                    status_code=401
                )

            # 用户活跃校验（与 JWT 分支同口径：禁用账号 403）
            user = db.get(User, record.user_code)
            if user is None or not user.is_active:
                throttle.record_failure(ip, prefix)
                middleware_logger.warning(
                    f"[AUTH] 403 - API Key 所属用户不可用，路径: {path}, "
                    f"user_code={record.user_code}")
                return JSONResponse(
                    {"code": 403, "message": "账号已被禁用，请联系管理员"},
                    status_code=403
                )

            # scope 路由注册表（default-deny）：API Key 仅放行显式注册的
            # scope 化端点；命中后再校验该 Key 是否持有所需 scope
            required_scope = scope_constants.match_api_key_route(request.method, path)
            if required_scope is None:
                middleware_logger.warning(
                    f"[AUTH] 403 - API Key 访问未注册端点，路径: {request.method} {path}")
                return JSONResponse(
                    {"code": 403, "message": "API Key 无权访问该端点"},
                    status_code=403
                )
            granted = scope_constants.normalize_scopes(record.scopes)
            if required_scope not in granted:
                middleware_logger.warning(
                    f"[AUTH] 403 - API Key 缺少 scope {required_scope}，路径: {path}, "
                    f"ip: {ip}")
                return JSONResponse(
                    {"code": 403, "message": f"API Key 缺少所需 scope：{required_scope}"},
                    status_code=403
                )

            # 认证成功：刷新 last_used_at（审计口径），提取用户属性
            record.last_used_at = datetime.now(timezone.utc)
            db.commit()
            user_dict = _build_user_dict(user)

        request.state.user = user_dict
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = granted

        return await call_next(request)
