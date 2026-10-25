"""僵尸扫描：清理过期租约、超时等待者、刷新峰值、清理旧审计、配置 DB→Redis 对账"""
import logging
import socket
import threading
import time
import uuid
from typing import Optional

from .config import load_config, reconcile_config_to_redis
from .engine import Engine
from .redis_keys import leases_key, peak_key, scan_lock_key

logger = logging.getLogger(__name__)

_LOCK_TTL_SECONDS = 90
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class JudgePoolScanner:
    def __init__(self, client=None):
        self._client = client
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _client_or_default(self):
        if self._client is None:
            from .client import get_pool_client

            self._client = get_pool_client()
        return self._client

    def _redis(self):
        from app.services.redis_stop_signal import get_redis_client

        return get_redis_client()

    def _acquire_lock(self) -> Optional[str]:
        token = f"{socket.gethostname()}:{uuid.uuid4()}"
        try:
            ok = self._redis().set(scan_lock_key(), token, nx=True, ex=_LOCK_TTL_SECONDS)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[judge_pool] 扫描锁获取失败: {e}")
            return None
        return token if ok else None

    def _release_lock(self, token: str) -> None:
        try:
            self._redis().eval(_RELEASE_LOCK_SCRIPT, 1, scan_lock_key(), token)
        except Exception:  # noqa: BLE001
            pass

    def run_once(self) -> None:
        token = self._acquire_lock()
        if token is None:
            return
        try:
            # 先做配置对账（DB 是持久化源）：save_config 的 Redis 写失败会留下
            # 「DB 新值 / Redis 旧值」的永久分叉，load_config 只在 Redis 为空时
            # 才回落 DB，所以必须由这里把 Redis 拉回 DB 值
            try:
                reconcile_config_to_redis()
            except Exception as e:  # noqa: BLE001 - 对账失败不影响僵尸清理
                logger.warning(f"[judge_pool] 配置对账跳过: {e}")
            cfg = load_config()
            client = self._client_or_default()
            redis = self._redis()
            for engine in Engine:
                client.prune_expired(engine)
                try:
                    active = redis.zcard(leases_key(engine))
                    redis.set(peak_key(engine), str(active), ex=120)
                except Exception:  # noqa: BLE001
                    pass
            try:
                from app.db.session import SessionLocal
                from .audit import cleanup_old_audit

                with SessionLocal() as db:
                    cleanup_old_audit(db, cfg.audit_retention_days)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[judge_pool] 审计清理跳过: {e}")
        finally:
            self._release_lock(token)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="judge-pool-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            interval = max(10, load_config().scan_interval_seconds)
            if self._stop.wait(interval):
                break
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[judge_pool] 扫描执行异常: {e}")


_scanner: Optional[JudgePoolScanner] = None
_scanner_lock = threading.Lock()


def get_scanner() -> JudgePoolScanner:
    global _scanner
    if _scanner is None:
        with _scanner_lock:
            if _scanner is None:
                _scanner = JudgePoolScanner()
                _scanner.start()
    return _scanner
