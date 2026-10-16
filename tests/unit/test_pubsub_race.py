"""
W5 PubSub 订阅竞态回归测试（整改计划 §2「W5 测试设计」竞态回归小节）

复现的旧缺陷（657fc49 及之前）：PubSubListener.__init__ 创建的 pubsub 对象
会被 _listen 线程启动后的重建覆盖；subscribe 若发生在重建完成前（典型场景：
首个 WS 连接 start_pubsub_listener 返回后立即 subscribe），订阅落在废弃
对象上且 _subscribed_channels 已登记 → 首个订阅静默丢失，客户端收不到推送。

测试设计（按计划）：并发 start_pubsub_listener + 立即 subscribe，用可控
sleep（patch _get_redis_connection 放大 _listen 重建窗口）复现旧竞态窗口，
断言消息可收到。

真实 Redis 跟随 settings.REDIS_URL 解析（频道用 w5race_ 前缀避免与并行
测试互扰；本地 dev 栈常为 6380，CI 为 6379，与被测监听器同实例）；
Redis 不可达时明确 skip（§5.1：跑不了明确 skip，不许假绿）。
"""
import asyncio
import json
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

import redis as redis_lib

pytestmark = pytest.mark.unit

# 可控 sleep：放大 _listen 重建 pubsub 的竞态窗口（旧实现的窗口极窄，直接
# 并发难以稳定复现；0.3s 足以覆盖 start 后立即 subscribe 的交错）
REBUILD_SLEEP_SECONDS = 0.3


@pytest.fixture
def race_redis():
    """被测 PubSubListener 实际连接的 Redis（settings.REDIS_URL 同源解析：
    环境变量 > 主仓 .env）；不可达明确 skip（不假绿）

    不能硬编码端口：本地 dev 栈常把 Redis 映射到非 6379 端口（6379 可能
    被其他项目占用），PubSubListener 连 settings.REDIS_URL 指向的实例，
    本 fixture 必须查同一实例，否则订阅可见性检查永远看不到对端写入。
    """
    from urllib.parse import urlparse

    from app.core.config import get_settings

    url = get_settings().REDIS_URL
    try:
        client = redis_lib.from_url(url, decode_responses=True)
        client.ping()
    except Exception as e:  # noqa: BLE001 - 探测失败统一 skip
        pytest.skip(f"Redis {urlparse(url).netloc or url} 不可达，竞态回归无法执行（{e}）")
    yield client
    client.close()


@pytest.fixture
def slow_rebuild(monkeypatch):
    """可控 sleep：放大 _listen 重建 pubsub 的窗口，稳定复现旧竞态交错"""
    from app.api.pubsub_handler import PubSubListener

    original = PubSubListener._get_redis_connection

    def slowed(self):
        time.sleep(REBUILD_SLEEP_SECONDS)
        return original(self)

    monkeypatch.setattr(PubSubListener, "_get_redis_connection", slowed)


class TestPubSubRace:
    def test_first_subscriptions_survive_listener_rebuild(self, race_redis, slow_rebuild):
        """并发 start_pubsub_listener + 立即 subscribe：监听线程重建 pubsub 期间
        落下的订阅不能丢——两频道消息都必须能经 broadcast 回调收到。

        交错设计（两个旧竞态窗口同时命中）：
        ① 监听线程 start 之前 subscribe（落在 __init__ 创建的 pubsub 上，
          随后被 _listen 重建覆盖——旧实现不重放，静默丢失）；
        ② start 之后、重建完成前（slow_rebuild 放大窗口）立即 subscribe 第二个频道。
        """
        from app.api.pubsub_handler import PubSubListener

        suffix = uuid.uuid4().hex[:8]
        task_a, task_b = f"w5race_a_{suffix}", f"w5race_b_{suffix}"
        channel_a, channel_b = f"task_status:{task_a}", f"task_status:{task_b}"
        received: dict[str, list] = {}

        async def broadcast_cb(task_id: str, message: dict):
            received.setdefault(task_id, []).append(message)

        # _broadcast_to_websocket 通过 run_coroutine_threadsafe 调度到事件循环，
        # 需要一个真正在跑的后台 loop（不跑则 future 永不完成、回调不执行）
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        listener = PubSubListener(broadcast_cb)
        listener.set_loop(loop)
        try:
            # 竞态窗口①：监听线程启动前订阅
            listener.subscribe(task_a)
            # 启动监听线程：_listen 将重建 pubsub 并（修复后）重放已登记频道
            listener.start()
            # 竞态窗口②：重建进行中立即订阅第二个频道
            listener.subscribe(task_b)

            # 等两个频道在 Redis 侧可见订阅者（subscribe 命令真实到达服务器）
            deadline = time.time() + 10
            while time.time() < deadline:
                channels = set(race_redis.pubsub_channels("task_status:w5race_*"))
                if {channel_a, channel_b} <= channels:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("两个频道未在 10s 内完成订阅（subscribe 未生效）")

            # 覆盖被放大的重建窗口，确保 _listen 的重建+重放已经完成
            time.sleep(REBUILD_SLEEP_SECONDS + 0.2)

            # 发布并带重试收集：旧实现下订阅落在被丢弃的旧 pubsub 对象上，
            # 监听线程只轮询新对象，无论重发多少次都收不到（红）；
            # 修复后重放完成即可收到（绿）。
            for seq in range(15):
                race_redis.publish(channel_a, json.dumps({"seq": seq, "from": "a"}))
                race_redis.publish(channel_b, json.dumps({"seq": seq, "from": "b"}))
                if task_a in received and task_b in received:
                    break
                time.sleep(0.2)

            assert task_a in received, (
                f"频道 {channel_a} 的订阅静默丢失（竞态未修复）：received={received}"
            )
            assert task_b in received, (
                f"频道 {channel_b} 的订阅静默丢失（竞态未修复）：received={received}"
            )
            assert any(m.get("from") == "a" for m in received[task_a])
            assert any(m.get("from") == "b" for m in received[task_b])
        finally:
            listener.stop()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            loop.close()

    def test_subscribe_redis_failure_not_registered(self):
        """subscribe 的 redis 命令失败：记日志、不抛出、且不登记
        _subscribed_channels——避免『已登记但实际未订阅』的静默丢失语义。"""
        from app.api.pubsub_handler import PubSubListener

        listener = PubSubListener()  # Redis 不可达时 __init__ 内部自捕获，pubsub=None
        real_pubsub = listener._pubsub
        fake_pubsub = MagicMock()
        fake_pubsub.subscribe.side_effect = redis_lib.exceptions.ConnectionError("boom")
        listener._pubsub = fake_pubsub
        try:
            # 旧实现此处会向上抛异常炸掉端点；修复后吞掉并保持未登记
            listener.subscribe("w5race_fail_t1")

            assert "w5race_fail_t1" not in listener._subscribed_channels
            fake_pubsub.subscribe.assert_called_once_with("task_status:w5race_fail_t1")
        finally:
            listener._pubsub = real_pubsub
            if real_pubsub is not None:
                real_pubsub.close()
