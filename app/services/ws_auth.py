"""
WebSocket 鉴权服务

WebSocket 请求不经过 AuthMiddleware（中间件对 websocket 直接透传，见
app/middleware/auth_middleware.py 的 dispatch），所有 WS 端点必须在
accept() 之前通过本模块完成 JWT 鉴权，未认证连接统一以 4401 关闭。

鉴权链路与 HTTP 侧完全一致：decode_token 验证 Bearer JWT（握手 query 参数
?token= 携带）→ 加载本地用户 → 返回与 auth_middleware 写入 request.state.user
同构的用户字典（user_code / org_code / super_admin_flag 等），供 WS 端点
做归属校验（W9 复用同一口径做全量 IDOR 收口）。
"""
import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket

from app.db.session import SessionLocal
from app.models.user_models import User
from app.services.jwt_service import decode_token

logger = logging.getLogger(__name__)

# WS 关闭码（应用自定义区间 4xxx，语义对齐 HTTP 状态码）
WS_CLOSE_UNAUTHORIZED = 4401  # 未提供认证信息 / token 无效或已过期（同 401）
WS_CLOSE_FORBIDDEN = 4403     # 归属校验失败 / 账号已被禁用（同 403）
WS_CLOSE_NOT_FOUND = 4404     # 目标任务不存在（同 404）


def extract_ws_token(websocket: WebSocket) -> str:
    """从握手 query 参数 ?token= 提取 JWT（握手阶段无法自定义 header）"""
    return websocket.query_params.get("token") or ""


async def require_ws_user(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """
    WebSocket 鉴权：验证本地 JWT 并返回用户字典。

    复用 HTTP 侧同一套用户字典结构（app.middleware.auth_middleware），
    供 WS 端点做归属校验（W9/W19 同一口径）。

    任一环节失败都会向客户端发送 close 并返回 None；
    调用方收到 None 后必须立即 return，不得继续 accept。
    """
    # 步骤1: 提取并验证 JWT
    payload = decode_token(extract_ws_token(websocket))
    if not payload or not payload.get("sub"):
        logger.warning("[WS-AUTH] 4401 - token 无效或已过期")
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    # 步骤2: 加载本地用户并提取属性为普通字典（与 auth_middleware 一致，避免 DetachedInstanceError）
    try:
        with SessionLocal() as db:
            user = db.get(User, payload.get("sub"))

            if not user:
                logger.warning(f"[WS-AUTH] 4401 - 用户不存在: {payload.get('sub')}")
                await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
                return None

            # W19：本地禁用的账号拒绝连接（与 auth_middleware 的 403 同口径）
            if not user.is_active:
                logger.warning(
                    f"[WS-AUTH] 4403 - 账号已被禁用: user_code={user.user_code}"
                )
                await websocket.close(code=WS_CLOSE_FORBIDDEN)
                return None

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
    except Exception as e:
        logger.error(f"[WS-AUTH] 4401 - 用户读取失败: {e}")
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    return user_dict
