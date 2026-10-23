"""数据接入 API（M8 QE8-4，OTel 兼容导入）

``POST /api/v1/ingest/otel-traces``：OTLP/HTTP JSON（``{"resourceSpans"}``）
或 Phoenix / Langfuse 导出 JSON 直接推送，转为 agent 会话数据集
（一 trace 一行，session_messages 进既有 trace 评测链路）。

定位（与 docs/user_guide 口径一致）：平台**不是 tracing 后端**——本端点是
单向的「tracing 资产 → 评测样本」入口，与 Langfuse/Phoenix/Jaeger 等
tracing 平台共存（trace 继续在原平台存查看板，评测样本按需导入本平台）。

请求形态：body 即 trace 载荷本身（application/json），导入选项走 query
参数（dataset_name 新建必填 / append_to 追加既有数据集 / description）。
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.access_control import (
    ensure_dataset_access,
    ensure_permission,
    user_team_code,
)
from app.services.audit_service import audited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["数据接入"])


@router.post(
    "/otel-traces",
    summary="OTel/Phoenix/Langfuse trace 导入为 agent 会话数据集",
    dependencies=[Depends(audited("CREATE", "dataset"))],
)
def ingest_otel_traces(
    request: Request,
    payload: dict = Body(
        ..., description="trace 载荷：OTLP/HTTP JSON（resourceSpans）或 Phoenix 导出（traceId+spans / data 数组）或 Langfuse 导出（traces 数组）"),
    dataset_name: str = Query(
        "", description="新建数据集名称（append_to 为空时必填"),
    append_to: str = Query(
        "", description="追加模式：目标数据集 ID（跨团队 403）"),
    description: str = Query("", description="数据集描述"),
    db: Session = Depends(get_db),
):
    """trace 载荷 → agent 会话数据集（M8 QE8-4，一 trace 一行）

    导入结果行含完整 session_messages（LLM 消息 + 工具 span 序列），
    直接进既有 trace 评测链路（长会话 Agent 评测 / 插件评测 trace 口径 /
    轨迹断言）。单次上限 200 trace / 5000 span，超限 400 分批提交。
    """
    from app.services.trace_ingest_service import TraceIngestError, ingest_trace_payload

    ensure_permission(request.state.user, "datasets")  # RBAC（QE4-3）：viewer 403
    # 追加模式目标数据集归属校验（与 confirm_session_import 同口径）
    if append_to:
        ensure_dataset_access(db, request.state.user, append_to)
    elif not dataset_name.strip():
        raise HTTPException(
            status_code=400,
            detail="请指定 dataset_name（新建数据集）或 append_to（追加既有数据集）")

    user = request.state.user
    user_code = user.get("user_code") if isinstance(user, dict) else getattr(user, "user_code", None)
    try:
        result = ingest_trace_payload(
            db, payload,
            name=dataset_name,
            description=description,
            real_dataset_id=append_to or None,
            created_by=user_code,
            team_code=user_team_code(user),
        )
    except TraceIngestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request.state.audit_resource = {
        "id": result["dataset_id"],
        "detail": {"count": result["count"], "format": result["format"],
                   "mode": "trace_ingest"},
    }
    return {
        "dataset_id": result["dataset_id"],
        "count": result["count"],
        "format": result["format"],
        "traces": result["traces"],
        "message": f"成功导入 {result['traces']} 条 trace 为 {result['count']} 行会话数据",
    }
