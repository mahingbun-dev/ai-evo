"""审计事件：有界队列 + 后台批量落库，绝不阻塞裁判调用"""
import atexit
import logging
import os
import queue
import threading
import time
from typing import Optional

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
_DROPPED = [0]
_writer_started = False
_writer_lock = threading.Lock()
_BATCH_SIZE = 200
_FLUSH_INTERVAL_SECONDS = 1.0


def record_event(event: str, *, engine: str, lease_id: str = "",
                 eval_task_id: Optional[str] = None,
                 celery_task_id: Optional[str] = None,
                 row_idx: Optional[int] = None,
                 wait_ms: Optional[int] = None,
                 hold_ms: Optional[int] = None,
                 detail: Optional[dict] = None) -> None:
    _ensure_writer()
    payload = {
        "event": event, "engine": engine, "lease_id": lease_id,
        "eval_task_id": eval_task_id, "celery_task_id": celery_task_id,
        "row_idx": row_idx, "wait_ms": wait_ms, "hold_ms": hold_ms,
        "host": os.environ.get("HOSTNAME") or "",
        "pid": os.getpid(), "detail": detail,
    }
    try:
        _QUEUE.put_nowait(payload)
    except queue.Full:
        _DROPPED[0] += 1


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        t = threading.Thread(target=_writer_loop, name="judge-pool-audit", daemon=True)
        t.start()
        _writer_started = True
        atexit.register(flush_audit)


def _writer_loop() -> None:
    batch = []
    last_flush = time.monotonic()
    while True:
        timeout = max(0.05, _FLUSH_INTERVAL_SECONDS - (time.monotonic() - last_flush))
        try:
            item = _QUEUE.get(timeout=timeout)
        except queue.Empty:
            item = None
        if item is not None and item.get("__flush__"):
            if batch:
                _write_batch(batch)
                last_flush = time.monotonic()
            item["__flush_event__"].set()
            continue
        if item is not None:
            batch.append({k: v for k, v in item.items() if not k.startswith("__")})
        if batch and (len(batch) >= _BATCH_SIZE or time.monotonic() - last_flush >= _FLUSH_INTERVAL_SECONDS):
            _write_batch(batch)
            last_flush = time.monotonic()


def _write_batch(batch: list) -> None:
    from app.models.judge_pool_models import JudgePoolAuditLog

    db = SessionLocal()
    try:
        for item in batch:
            db.add(JudgePoolAuditLog(**item))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[judge_pool] 审计落库失败，丢弃 {len(batch)} 条: {e}")
        db.rollback()
    finally:
        db.close()
    batch.clear()


def flush_audit(timeout: float = 2.0) -> None:
    """测试与进程退出用：投递屏障，等 writer 提交完当前批次"""
    done = threading.Event()
    try:
        _QUEUE.put({"__flush__": True, "__flush_event__": done}, timeout=timeout)
    except queue.Full:
        return
    done.wait(timeout)


def query_audit(db, engine=None, event=None, start=None, end=None, skip: int = 0, limit: int = 50):
    from app.models.judge_pool_models import JudgePoolAuditLog

    query = db.query(JudgePoolAuditLog)
    if engine:
        query = query.filter(JudgePoolAuditLog.engine == engine)
    if event:
        query = query.filter(JudgePoolAuditLog.event == event)
    if start:
        query = query.filter(JudgePoolAuditLog.created_at >= start)
    if end:
        query = query.filter(JudgePoolAuditLog.created_at <= end)
    total = query.count()
    rows = query.order_by(JudgePoolAuditLog.created_at.desc()).offset(skip).limit(limit).all()
    return rows, total


def cleanup_old_audit(db, retention_days: int) -> int:
    from datetime import datetime, timedelta, timezone

    from app.models.judge_pool_models import JudgePoolAuditLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = db.query(JudgePoolAuditLog).filter(JudgePoolAuditLog.created_at < cutoff).delete()
    db.commit()
    return int(deleted or 0)
