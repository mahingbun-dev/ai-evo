"""每进程一个心跳线程：租约续约 + 持有线程死亡回收

续约前先看持有者线程是否还活着：无限续约会把卡死线程的额度永久占住，
以线程存活为续约前提才能让死线程的租约尽快归还。
"""
import atexit
import logging
import threading
from typing import Callable, Dict, Optional, Tuple

from .config import load_config

logger = logging.getLogger(__name__)


class JudgePoolHeartbeat:
    def __init__(self, client=None, event_sink: Optional[Callable] = None):
        self._client = client
        self._event_sink = event_sink
        self._entries: Dict[str, Tuple[object, threading.Thread]] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._atexit_registered = False

    def _client_or_default(self):
        if self._client is None:
            from .client import get_pool_client

            self._client = get_pool_client()
        return self._client

    def _emit(self, event: str, lease, **kwargs) -> None:
        if self._event_sink is not None:
            self._event_sink(event, lease, **kwargs)
            return
        try:
            from .audit import record_event

            record_event(event, engine=lease.engine.value, lease_id=lease.lease_id, **kwargs)
        except Exception:  # noqa: BLE001
            pass

    def register(self, lease, owner_thread: threading.Thread) -> None:
        with self._lock:
            self._entries[lease.lease_id] = (lease, owner_thread)
        self._ensure_started()

    def unregister(self, lease_id: str) -> None:
        with self._lock:
            self._entries.pop(lease_id, None)

    def run_once(self) -> None:
        with self._lock:
            items = list(self._entries.items())
        client = self._client_or_default()
        for lease_id, (lease, owner_thread) in items:
            if not owner_thread.is_alive():
                client.release(lease)
                self.unregister(lease_id)
                self._emit("owner_thread_dead", lease)
                continue
            renewed = client.renew(lease)
            if renewed is None:
                # 传输异常（Redis 抖动）不等于租约失效：保持登记、下轮重试。
                # 这里判失效会让一次抖动跨过一个心跳周期就停掉本进程全部在飞
                # 租约的续约，TTL 后额度被回收而调用仍在跑 → 反向超发
                logger.debug("[judge_pool] 续约传输失败，保留登记下轮重试: %s", lease_id)
                continue
            if not renewed:
                # 标记失效：持有线程下一次 acquire 看到 lost 就不会误判为重入放行
                lease.lost = True
                self.unregister(lease_id)
                self._emit("lease_lost", lease)

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="judge-pool-heartbeat", daemon=True)
        self._thread.start()
        # 退出钩子只注册一次（心跳线程可能因异常退出后被重新拉起）
        if not self._atexit_registered:
            atexit.register(self.shutdown)
            self._atexit_registered = True

    def _run(self) -> None:
        while not self._stop.is_set():
            interval = max(1, load_config().heartbeat_interval_seconds)
            if self._stop.wait(interval):
                break
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - 心跳异常不能杀死线程
                logger.warning(f"[judge_pool] 心跳执行异常: {e}")

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        """进程退出：置位停止 + 批量释放本进程登记的全部租约

        只置位不释放的话，每次发布/优雅重启都会留下最长 lease_ttl（默认 180s）
        的幽灵占用 —— 额度要等 TTL 过期才被回收，期间新任务白等。
        逐条释放，单条异常吞掉（TTL 兜底），不能挡住其余租约。
        """
        self.stop()
        with self._lock:
            items = list(self._entries.items())
            self._entries.clear()
        if not items:
            return
        try:
            client = self._client_or_default()
        except Exception as e:  # noqa: BLE001 - 退出路径不抛，额度由 TTL 兜底回收
            logger.warning(f"[judge_pool] 退出时无法获取池客户端，租约等待 TTL 回收: {e}")
            return
        for lease_id, (lease, _owner_thread) in items:
            try:
                client.release(lease)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[judge_pool] 退出释放租约 {lease_id} 失败（等待 TTL 回收）: {e}")
        # 这批 release 事件要落库：audit 的 flush_audit 可能比本钩子先跑
        # （atexit 是 LIFO，谁后注册谁先跑），不主动 flush 就会随进程丢掉。
        # 只在写线程已起时 flush，避免测试/无审计场景白等超时
        try:
            from . import audit

            if audit._writer_started:
                audit.flush_audit(timeout=1.0)
        except Exception:  # noqa: BLE001 - 审计缺失不影响释放本身
            pass


_heartbeat: Optional[JudgePoolHeartbeat] = None
_lock = threading.Lock()


def get_heartbeat() -> JudgePoolHeartbeat:
    global _heartbeat
    if _heartbeat is None:
        with _lock:
            if _heartbeat is None:
                _heartbeat = JudgePoolHeartbeat()
    return _heartbeat
