"""
WebSocket API 端点 - 任务状态实时推送

提供 WebSocket 端点 /ws/task/{task_id}，处理连接建立、心跳、消息接收和连接关闭。
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.websocket_manager import ws_manager
from app.api.websocket_models import WebSocketConnectMessage, WebSocketPongMessage
from app.db.session import SessionLocal
from app.services.ws_auth import (
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_NOT_FOUND,
    require_ws_user,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_hub_import_job(task_id: str):
    """按 ID 查 hub 导入 job（不存在返回 None）"""
    from app.models.hub_file_models import HubImportJob

    with SessionLocal() as db:
        return db.get(HubImportJob, task_id)


def _hub_job_subscription_allowed(job, user: dict) -> bool:
    """hub 导入 job 的最小归属判断：创建人本人或超管

    HubImportJob 无团队字段（仅 created_by），口径取 creator 本人放行；
    API 直建且未带用户（created_by=None）的 job 仅超管可订阅。
    """
    if int(user.get("super_admin_flag") or 0) == 1:
        return True
    user_code = user.get("user_code")
    return bool(job.created_by) and user_code is not None and job.created_by == user_code


async def _check_task_ownership(task_id: str, user: dict, websocket: WebSocket) -> bool:
    """
    归属校验（W9 全量收口）：复用 access_control 统一口径——

    任务与归属在同一次查询结果上完成校验（防 TOCTOU），判定内核与
    HTTP 侧 ensure_task_access 严格同源（check_team_access：超管放行、
    team_code/org_code 空值统一按 DEFAULT 兜底）。校验失败直接关闭连接
    （4403 非本团队 / 4404 任务不存在）并返回 False，调用方收到 False
    后必须立即 return。

    W14 扩展：hub 批量导入 job（hubjob_ 前缀 ID）复用同一 WS 端点订阅
    task_status:{job_id} 频道的 hub_import_progress 进度推送——评测任务查无
    时回落 hub 导入 job 表，按「创建人本人或超管」放行；两表均查无才按
    4404 关闭。
    """
    from app.models.task_models import EvaluationTask
    from app.services.access_control import AccessDeniedError, check_team_access

    with SessionLocal() as db:
        task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
    if task is None:
        hub_job = _get_hub_import_job(task_id)
        if hub_job is None:
            logger.warning(f"[WS] 4404 - 任务不存在: task_id={task_id}")
            await websocket.close(code=WS_CLOSE_NOT_FOUND)
            return False
        if not _hub_job_subscription_allowed(hub_job, user):
            logger.warning(
                f"[WS] 4403 - hub 导入 job 跨用户订阅被拒: job_id={task_id}, "
                f"created_by={hub_job.created_by}, user={user.get('user_code')}"
            )
            await websocket.close(code=WS_CLOSE_FORBIDDEN)
            return False
        return True

    try:
        check_team_access(user, task.team_code)
    except AccessDeniedError:
        logger.warning(
            f"[WS] 4403 - 跨团队订阅被拒: task_id={task_id}, "
            f"task_team={task.team_code}, user_team={user.get('org_code')}"
        )
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return False

    return True


@router.websocket("/ws/task/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """
    WebSocket 端点 - 订阅任务状态更新

    连接时向客户端发送连接成功消息。
    客户端每 30 秒发送 ping 进行心跳，服务端回复 pong。

    鉴权：accept 之前完成 SSO Cookie 验证（未认证 4401），
    并做最小归属校验（非本团队 4403，任务不存在 4404）。
    """
    # 鉴权（必须在 accept 之前）：失败时 require_ws_user 已发送 close(4401)
    user = await require_ws_user(websocket)
    if user is None:
        return

    # 归属校验（W9 全量收口：与 HTTP 侧 access_control 同一口径）
    if not await _check_task_ownership(task_id, user, websocket):
        return

    # 启动 PubSubListener 并订阅该任务频道
    from app.api.pubsub_handler import start_pubsub_listener, get_pubsub_listener
    listener = start_pubsub_listener(ws_manager.broadcast)
    listener.set_loop(asyncio.get_event_loop())
    listener.subscribe(task_id)

    # 建立连接
    await ws_manager.connect(task_id, websocket)

    # 发送连接成功消息
    try:
        connect_msg = WebSocketConnectMessage(task_id=task_id)
        await websocket.send_json(connect_msg.model_dump())
    except Exception as e:
        logger.error(f"Failed to send connect message: {e}")
        await ws_manager.disconnect(task_id, websocket)
        return

    # 心跳时间戳
    last_heartbeat = asyncio.get_event_loop().time()

    try:
        while True:
            # 接收客户端消息
            data = await websocket.receive_text()

            # 处理心跳
            if data == "ping":
                last_heartbeat = asyncio.get_event_loop().time()
                pong_msg = WebSocketPongMessage()
                await websocket.send_json(pong_msg.model_dump())
            else:
                # 客户端不应主动发送业务消息
                logger.warning(f"Unexpected message from client: {data}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: task_id={task_id}")
    except Exception as e:
        logger.error(f"WebSocket error: task_id={task_id}, error={e}")
    finally:
        await ws_manager.disconnect(task_id, websocket)
