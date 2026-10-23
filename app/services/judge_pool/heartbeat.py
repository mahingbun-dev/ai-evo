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
            if not client.renew(lease):
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
        atexit.register(self.stop)

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


_heartbeat: Optional[JudgePoolHeartbeat] = None
_lock = threading.Lock()


def get_heartbeat() -> JudgePoolHeartbeat:
    global _heartbeat
    if _heartbeat is None:
        with _lock:
            if _heartbeat is None:
                _heartbeat = JudgePoolHeartbeat()
    return _heartbeat
