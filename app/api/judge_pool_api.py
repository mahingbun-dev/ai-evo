"""LLM 裁判并发池：监控、配置、强制释放与审计（仅超管）"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.access_control import ensure_super_admin
from app.services.judge_pool.audit import query_audit, record_event
from app.services.judge_pool.client import get_pool_client
from app.services.judge_pool.config import load_config, save_config
from app.services.judge_pool.engine import Engine, coerce_engine
from app.services.judge_pool.errors import JudgePoolError, JudgePoolRedisSyncError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/judge-pool", tags=["并发池"], dependencies=[Depends(ensure_super_admin)])

# 审计事件白名单：与 record_event 的调用点保持一致
_AUDIT_EVENTS = {
    "acquire", "wait_timeout", "release", "lease_expired", "redis_unavailable",
    "manual_release", "owner_thread_dead", "lease_lost",
}


class JudgePoolConfigPayload(BaseModel):
    graph_limit: Optional[int] = Field(None, ge=1, le=128)
    claude_agent_limit: Optional[int] = Field(None, ge=1, le=128)
    harness_limit: Optional[int] = Field(None, ge=1, le=128)
    plugin_limit: Optional[int] = Field(None, ge=1, le=128)
    lease_ttl_seconds: Optional[int] = Field(None, ge=30, le=3600)
    heartbeat_interval_seconds: Optional[int] = Field(None, ge=5, le=600)
    wait_timeout_seconds: Optional[int] = Field(None, ge=10, le=7200)
    scan_interval_seconds: Optional[int] = Field(None, ge=10, le=600)
    audit_retention_days: Optional[int] = Field(None, ge=1, le=365)
    fail_open: Optional[bool] = None


def _current_operator(request: Request) -> str:
    """操作人标识：登录上下文可能只有 username 而没有 user_code，两处都取"""
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user.get("user_code") or user.get("username") or ""
    return getattr(user, "user_code", "") or ""


def _coerce_engine_or_400(value: str) -> Engine:
    try:
        return coerce_engine(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/status")
def get_status(db: Session = Depends(get_db)) -> Dict[str, Any]:
    # Redis 故障时 snapshot 不抛异常、返回 degraded 标记：监控页仍需 200 出数据，
    # 由前端渲染橙色降级横幅（返回 503 会让页面无内容可展示）
    data = get_pool_client().snapshot()
    leases = data.get("leases") or []
    task_ids = {item.get("eval_task_id") for item in leases if item.get("eval_task_id")}
    names: Dict[str, str] = {}
    if task_ids:
        # 任务名只是展示增强，DB 查询失败不能把监控接口打成 500
        try:
            from app.models.task_models import EvaluationTask

            rows = db.query(EvaluationTask.id, EvaluationTask.name).filter(
                EvaluationTask.id.in_(task_ids)
            ).all()
            names = {row[0]: row[1] for row in rows}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[judge_pool] 补全租约任务名失败: {e}")
    for item in leases:
        item["task_name"] = names.get(item.get("eval_task_id"), "")
    return data


@router.get("/config")
def get_config() -> Dict[str, Any]:
    return load_config(force=True).to_dict()


@router.put("/config")
def update_config(payload: JudgePoolConfigPayload, request: Request) -> Dict[str, Any]:
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    # 关系校验（心跳 < 租约一半）由 save_config 在 DB 现值合并之后做，
    # 端点不再用 load_config() 预检，避免出现两套校验基准
    try:
        cfg = save_config(values, updated_by=_current_operator(request), strict=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except JudgePoolRedisSyncError as e:
        # DB 已是可靠来源，允许 200 但如实告知未同步
        return {**e.config.to_dict(), "redis_synced": False}
    except JudgePoolError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {**cfg.to_dict(), "redis_synced": True}


def _redis_unavailable(e: Exception) -> HTTPException:
    logger.error(f"[judge_pool] 强制释放时 Redis 不可用: {e}")
    return HTTPException(status_code=503, detail="Redis 不可用，无法确认租约状态，请稍后重试")


@router.post("/leases/{lease_id}/release")
def force_release(lease_id: str, request: Request, engine: str = Query(...)) -> Dict[str, Any]:
    eng = _coerce_engine_or_400(engine)
    try:
        released = get_pool_client().force_release(eng, lease_id, strict=True)
    except RedisError as e:
        raise _redis_unavailable(e) from e
    # 租约已不存在时不能留一条「释放过」的审计，否则审计与事实不符
    if released:
        record_event(
            "manual_release",
            engine=eng.value,
            lease_id=lease_id,
            detail={"scope": "single", "operator": _current_operator(request)},
        )
    return {"released": released}


@router.post("/release-all")
def release_all(request: Request, engine: Optional[str] = Query(None)) -> Dict[str, Any]:
    operator = _current_operator(request)
    if engine:
        eng = _coerce_engine_or_400(engine)
        try:
            count = get_pool_client().force_release_engine(eng, strict=True)
        except RedisError as e:
            raise _redis_unavailable(e) from e
        if count:
            record_event(
                "manual_release",
                engine=eng.value,
                detail={"scope": "all", "count": count, "operator": operator},
            )
        return {"released": count}

    total = 0
    for eng in Engine:
        try:
            count = get_pool_client().force_release_engine(eng, strict=True)
        except RedisError as e:
            raise _redis_unavailable(e) from e
        total += count
        if count:
            record_event(
                "manual_release",
                engine=eng.value,
                detail={"scope": "all", "count": count, "operator": operator},
            )
    return {"released": total}


@router.get("/audit")
def list_audit(
    db: Session = Depends(get_db),
    engine: Optional[str] = None,
    event: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    if engine:
        engine = _coerce_engine_or_400(engine).value
    if event and event not in _AUDIT_EVENTS:
        raise HTTPException(status_code=400, detail=f"未知审计事件: {event!r}")
    rows, total = query_audit(db, engine=engine, event=event, start=start, end=end,
                              skip=(page - 1) * page_size, limit=page_size)
    return {
        "total": total,
        "items": [
            {
                "id": r.id, "event": r.event, "engine": r.engine, "lease_id": r.lease_id,
                "eval_task_id": r.eval_task_id, "row_idx": r.row_idx,
                "wait_ms": r.wait_ms, "hold_ms": r.hold_ms,
                "host": r.host, "pid": r.pid, "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
