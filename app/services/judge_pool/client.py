"""并发池客户端：申请、释放、续约、快照与清理"""
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import lua
from .audit import record_event
from .config import load_config
from .context import clear_current_lease, current_judge_context, current_lease, set_current_lease
from .engine import Engine, coerce_engine
from .errors import JudgePoolAborted, JudgePoolTimeout
from .heartbeat import get_heartbeat
from .redis_keys import lease_data_key, leases_key, peak_key, wait_key

logger = logging.getLogger(__name__)


@dataclass
class Lease:
    engine: Engine
    lease_id: str
    acquired_at_ms: int
    meta: Dict[str, Any] = None
    reentrant: bool = False
    unavailable: bool = False


@dataclass
class AcquireResult:
    acquired: bool
    active: int
    expired_details: List[str]
    position: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redis_client():
    from app.services.redis_stop_signal import get_redis_client

    return get_redis_client()


class JudgePoolClient:
    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._lock = threading.Lock()

    def _client(self):
        if self._redis is None:
            with self._lock:
                if self._redis is None:
                    self._redis = _redis_client()
        return self._redis

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def acquire(self, engine, meta: Optional[dict] = None, wait_timeout: Optional[int] = None,
                stop_checker=None) -> Lease:
        engine = coerce_engine(engine)
        ctx = current_judge_context()
        stop_checker = stop_checker or ctx.stop_checker
        existing = current_lease(engine)
        if existing is not None:
            return replace(existing, reentrant=True)

        cfg = load_config()
        wait_timeout = cfg.wait_timeout_seconds if wait_timeout is None else wait_timeout
        lease_id = str(uuid.uuid4())
        started = time.monotonic()
        detail = {
            "lease_id": lease_id,
            "engine": engine.value,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "eval_task_id": ctx.eval_task_id,
            "celery_task_id": ctx.celery_task_id,
            "row_idx": ctx.row_idx,
            "acquired_at": _now_ms(),
            "last_renew_at": _now_ms(),
            "meta": meta or {},
        }
        deadline = started + wait_timeout
        backoff = 0.2

        while True:
            if stop_checker is not None and stop_checker.is_set():
                self._drop_waiter(engine, lease_id)
                raise JudgePoolAborted(f"并发池等待被中止（引擎 {engine.value}）")
            try:
                result = self._try_acquire(engine, lease_id, detail, cfg)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[judge_pool] Redis 不可用，放行调用: {e}")
                record_event("redis_unavailable", engine=engine.value, lease_id=lease_id,
                             eval_task_id=ctx.eval_task_id, row_idx=ctx.row_idx,
                             detail={"error": str(e)[:500]})
                if not cfg.fail_open:
                    raise
                return Lease(engine, lease_id, detail["acquired_at"], meta, unavailable=True)

            self._record_expired(engine, result.expired_details)

            if result.acquired:
                lease = Lease(engine, lease_id, detail["acquired_at"], meta)
                set_current_lease(engine, lease)
                get_heartbeat().register(lease, threading.current_thread())
                record_event("acquire", engine=engine.value, lease_id=lease_id,
                             eval_task_id=ctx.eval_task_id, celery_task_id=ctx.celery_task_id,
                             row_idx=ctx.row_idx, wait_ms=int((time.monotonic() - started) * 1000),
                             detail={"position": result.position})
                return lease

            if time.monotonic() >= deadline:
                self._drop_waiter(engine, lease_id)
                record_event("wait_timeout", engine=engine.value, lease_id=lease_id,
                             eval_task_id=ctx.eval_task_id, row_idx=ctx.row_idx,
                             wait_ms=int((time.monotonic() - started) * 1000),
                             detail={"position": result.position, "active": result.active})
                raise JudgePoolTimeout(
                    f"并发池等待超时（引擎 {engine.value}，等待 {wait_timeout}s，"
                    f"队列位次 {result.position}，活跃 {result.active}）"
                )
            self._sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)

    def release(self, lease: Lease) -> bool:
        if lease is None or lease.unavailable or lease.reentrant:
            return False
        clear_current_lease(lease.engine, lease)
        try:
            get_heartbeat().unregister(lease.lease_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            removed = self._lua_release(lease.engine, lease.lease_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[judge_pool] 释放租约失败（等待 TTL 回收）: {e}")
            return False

        record_event("release", engine=lease.engine.value, lease_id=lease.lease_id,
                     hold_ms=_now_ms() - lease.acquired_at_ms)
        return bool(removed)

    def renew(self, lease: Lease) -> bool:
        if lease is None or lease.unavailable or lease.reentrant:
            return False
        cfg = load_config()
        detail = {"lease_id": lease.lease_id, "engine": lease.engine.value,
                  "last_renew_at": _now_ms()}
        try:
            return bool(self._lua_renew(lease.engine, lease.lease_id, json.dumps(detail, ensure_ascii=False), cfg))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[judge_pool] 续约失败: {e}")
            return False

    def force_release(self, engine, lease_id: str) -> bool:
        engine = coerce_engine(engine)
        try:
            removed = self._lua_release(engine, lease_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[judge_pool] 强制释放失败: {e}")
            return False
        return bool(removed)

    def force_release_engine(self, engine) -> int:
        engine = coerce_engine(engine)
        try:
            ids = self._client().zrange(leases_key(engine), 0, -1)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[judge_pool] 读取租约失败: {e}")
            return 0
        return sum(1 for lease_id in ids if self.force_release(engine, lease_id))

    def prune_expired(self, engine) -> List[str]:
        engine = coerce_engine(engine)
        cfg = load_config()
        now = _now_ms()
        try:
            res = self._client().eval(
                lua.PRUNE_SCRIPT, 3,
                leases_key(engine), lease_data_key(engine), wait_key(engine),
                now, now - cfg.wait_timeout_seconds * 1000,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[judge_pool] 清理过期租约失败: {e}")
            return []
        details = res[0] or []
        self._record_expired(engine, details)
        return details

    def snapshot(self) -> Dict[str, Any]:
        cfg = load_config()
        out: Dict[str, Any] = {"redis_available": True, "degraded": False, "engines": {}, "leases": []}
        try:
            client = self._client()
            for engine in Engine:
                active = client.zcard(leases_key(engine))
                waiting = client.zcard(wait_key(engine))
                raw_ids = client.zrange(leases_key(engine), 0, -1)
                details = client.hmget(lease_data_key(engine), raw_ids) if raw_ids else []
                for raw in details:
                    if not raw:
                        continue
                    item = json.loads(raw)
                    item["ttl_remaining_seconds"] = max(
                        0, int((client.zscore(leases_key(engine), item["lease_id"]) or 0) - _now_ms()) // 1000
                    )
                    out["leases"].append(item)
                peak_raw = client.get(peak_key(engine)) or "0"
                out["engines"][engine.value] = {
                    "limit": cfg.limit_for(engine),
                    "active": active,
                    "available": max(0, cfg.limit_for(engine) - active),
                    "waiting": waiting,
                    "peak_1m": int(peak_raw),
                }
        except Exception as e:  # noqa: BLE001
            out["redis_available"] = False
            out["degraded"] = True
            out["error"] = str(e)[:500]
        return out

    # ---------- 脚本调用（测试可覆盖） ----------

    def _try_acquire(self, engine, lease_id, detail, cfg) -> AcquireResult:
        now = _now_ms()
        res = self._client().eval(
            lua.ACQUIRE_SCRIPT, 3,
            leases_key(engine), lease_data_key(engine), wait_key(engine),
            now, cfg.lease_ttl_seconds * 1000, cfg.limit_for(engine),
            lease_id, lease_id, json.dumps(detail, ensure_ascii=False),
            now - cfg.wait_timeout_seconds * 1000,
        )
        return AcquireResult(bool(res[0]), int(res[1]), list(res[2] or []), int(res[3]))

    def _lua_release(self, engine, lease_id) -> int:
        res = self._client().eval(
            lua.RELEASE_SCRIPT, 2, leases_key(engine), lease_data_key(engine), lease_id
        )
        return int(res[0])

    def _lua_renew(self, engine, lease_id, detail_json, cfg) -> int:
        return int(self._client().eval(
            lua.RENEW_SCRIPT, 2, leases_key(engine), lease_data_key(engine),
            lease_id, _now_ms() + cfg.lease_ttl_seconds * 1000, detail_json,
        ))

    def _drop_waiter(self, engine, waiter_id) -> None:
        try:
            self._client().zrem(wait_key(engine), waiter_id)
        except Exception:  # noqa: BLE001
            pass

    def _record_expired(self, engine, details: List[str]) -> None:
        if not details:
            return
        for raw in details:
            try:
                item = json.loads(raw)
            except Exception:  # noqa: BLE001
                item = {"raw": str(raw)[:500]}
            record_event("lease_expired", engine=engine.value,
                         lease_id=item.get("lease_id", ""),
                         eval_task_id=item.get("eval_task_id"),
                         row_idx=item.get("row_idx"),
                         detail=item)


_client: Optional[JudgePoolClient] = None
_client_lock = threading.Lock()


def get_pool_client() -> JudgePoolClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = JudgePoolClient()
    return _client
