"""
WebSocket API 端点 - 任务状态实时推送

提供 WebSocket 端点 /ws/task/{task_id}，处理连接建立、心跳、消息接收和连接关闭。
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.websocket_manager import ws_manager
from app.api.websocket_models import WebSocketConnectMessage, WebSocketPongMessage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/task/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """
    WebSocket 端点 - 订阅任务状态更新

    连接时向客户端发送连接成功消息。
    客户端每 30 秒发送 ping 进行心跳，服务端回复 pong。
    """
    from app.services.jwt_service import decode_token
    if not decode_token(websocket.query_params.get("token") or ""):
        await websocket.close(code=4401)
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
