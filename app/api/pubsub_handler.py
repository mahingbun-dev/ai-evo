"""
Redis Pub/Sub 处理器 - 订阅任务状态更新并广播到 WebSocket 客户端

使用独立的 Redis 连接来订阅 task_status:{task_id} 频道，
收到消息时调用 WebSocketManager.broadcast() 推送给所有订阅客户端。
"""

import asyncio
import json
import logging
import queue
import threading
from typing import Optional

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 全局 Pub/Sub 监听器引用
_pubsub_listener: Optional["PubSubListener"] = None


class PubSubListener:
    """Redis Pub/Sub 监听器"""

    def __init__(self, websocket_broadcast_func=None):
        self._redis_url = get_settings().REDIS_URL
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._broadcast_func = websocket_broadcast_func
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 订阅命令队列
        self._subscribe_queue: queue.Queue = queue.Queue()
        # 已订阅的频道
        self._subscribed_channels: set[str] = set()
        # 创建 Redis pubsub 连接（在主线程中创建，供 subscribe/unsubscribe 同步使用）
        try:
            self._pubsub = self._get_redis_connection().pubsub()
        except Exception as e:
            logger.warning(f"Failed to create pubsub in __init__: {e}")
            self._pubsub = None

    def _get_redis_connection(self) -> redis.Redis:
        """获取独立的 Redis 连接"""
        return redis.from_url(self._redis_url, decode_responses=True)

    def _broadcast_to_websocket(self, task_id: str, message: dict) -> None:
        """广播消息到 WebSocket 的包装方法"""
        if not self._broadcast_func:
            logger.warning("No broadcast function configured")
            return
        if not self._loop:
            logger.warning("No event loop configured")
            return
        try:
            logger.info(f"Scheduling broadcast: task_id={task_id}")
            future = asyncio.run_coroutine_threadsafe(
                self._broadcast_func(task_id, message), self._loop
            )
            try:
                future.result(timeout=5)
                logger.info(f"Broadcast scheduled successfully")
            except Exception as e:
                logger.error(f"Broadcast future failed: {e}")
        except Exception as e:
            logger.error(f"Failed to broadcast to WebSocket: {e}")

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置事件循环，供广播回调使用"""
        self._loop = loop

    def subscribe(self, task_id: str) -> None:
        """订阅指定任务的状态更新频道"""
        if task_id in self._subscribed_channels:
            return
        channel = f"task_status:{task_id}"
        # 如果 pubsub 已创建，直接订阅；否则放入队列等监听线程处理
        if self._pubsub is not None:
            self._pubsub.subscribe(channel)
            self._subscribed_channels.add(task_id)
            logger.info(f"Subscribed to Redis channel: {channel}")
        else:
            self._subscribe_queue.put(("subscribe", task_id))
            logger.info(f"Subscribe request queued: task_id={task_id}")

    def unsubscribe(self, task_id: str) -> None:
        """取消订阅"""
        if task_id not in self._subscribed_channels:
            return
        channel = f"task_status:{task_id}"
        if self._pubsub is not None:
            self._pubsub.unsubscribe(channel)
            self._subscribed_channels.discard(task_id)
            logger.info(f"Unsubscribed from Redis channel: {channel}")
        else:
            self._subscribe_queue.put(("unsubscribe", task_id))

    def _process_subscribe_queue(self) -> None:
        """处理订阅请求队列 - 非阻塞，只处理队列中已有的请求"""
        try:
            while True:
                try:
                    cmd, task_id = self._subscribe_queue.get_nowait()
                except queue.Empty:
                    break
                channel = f"task_status:{task_id}"
                if cmd == "subscribe":
                    if task_id not in self._subscribed_channels:
                        self._pubsub.subscribe(channel)
                        self._subscribed_channels.add(task_id)
                        logger.info(f"Subscribed to Redis channel: {channel}")
                elif cmd == "unsubscribe":
                    if task_id in self._subscribed_channels:
                        self._pubsub.unsubscribe(channel)
                        self._subscribed_channels.discard(task_id)
                        logger.info(f"Unsubscribed from Redis channel: {channel}")
        except Exception as e:
            logger.error(f"Error processing subscribe queue: {e}")

    def _listen(self) -> None:
        """在独立线程中监听 Redis 消息"""
        # 创建独立的 Redis 连接供监听线程使用
        self._pubsub = self._get_redis_connection().pubsub()
        logger.info("Redis pubsub connection created in listener thread")

        logger.info("Starting Redis PubSub listener")
        message_count = 0
        while self._running:
            # 处理订阅请求
            self._process_subscribe_queue()

            # 使用 listen() 的超时版本，避免永久阻塞
            try:
                message = self._pubsub.get_message(timeout=0.5)
                if message:
                    message_count += 1
                    logger.info(f"Redis message #{message_count}: {message}")
                    if message["type"] == "message":
                        try:
                            channel = message["channel"]
                            task_id = channel.replace("task_status:", "")
                            data = json.loads(message["data"])
                            self._broadcast_to_websocket(task_id, data)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse Redis message: {e}")
                        except Exception as e:
                            logger.error(f"Error processing Redis message: {e}")
            except Exception as e:
                logger.error(f"Redis get_message error: {e}")

        logger.info("Redis PubSub listener stopped")

    def start(self) -> None:
        """启动 Pub/Sub 监听线程"""
        if self._running:
            return

        self._running = True
        self._listener_thread = threading.Thread(target=self._listen, daemon=True)
        self._listener_thread.start()
        logger.info("PubSubListener started")

    def stop(self) -> None:
        """停止 Pub/Sub 监听线程"""
        self._running = False
        if self._listener_thread:
            self._listener_thread.join(timeout=2)
            self._listener_thread = None
        if self._pubsub:
            try:
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None
        self._subscribed_channels.clear()
        logger.info("PubSubListener stopped")


def start_pubsub_listener(websocket_broadcast_func=None) -> "PubSubListener":
    """启动全局 Pub/Sub 监听器"""
    global _pubsub_listener
    if _pubsub_listener is None:
        _pubsub_listener = PubSubListener(websocket_broadcast_func)
        _pubsub_listener.start()
    return _pubsub_listener


def get_pubsub_listener() -> Optional["PubSubListener"]:
    """获取全局 Pub/Sub 监听器"""
    return _pubsub_listener


def stop_pubsub_listener() -> None:
    """停止全局 Pub/Sub 监听器"""
    global _pubsub_listener
    if _pubsub_listener:
        _pubsub_listener.stop()
        _pubsub_listener = None