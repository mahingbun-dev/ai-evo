"""trace 载荷导入服务（M8 QE8-4，OTel 兼容导入）

/ingest/otel-traces 的编排层：载荷格式识别（otelp/phoenix/langfuse，见
app/services/otel_trace_parser）→ 解析为 SessionRecord → 经
session_dataset_service._import_parsed_sessions 落为 agent 会话数据集
（一 trace 一行，session_messages 整包进既有 trace 评测链路：长会话
Agent 评测 / 插件评测 trace 口径 / 轨迹断言）。

限额（不可信大载荷防线，超限 400 而不是把 worker 内存拖垮）：
- 单请求 trace 数 ≤ MAX_TRACES_PER_IMPORT（一 trace 一数据行）；
- 单请求 span 总数 ≤ MAX_SPANS_PER_IMPORT（解析后的会话消息数近似
  span 数的 2 倍，限额在解析后按 metadata.span_count 汇总校验）。

定位（文档同口径）：单向「tracing 资产 → 评测样本」入口，平台不做
span 存储/检索/看板——与 Langfuse/Phoenix/Jaeger 等 tracing 平台共存。
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_TRACES_PER_IMPORT = 200
MAX_SPANS_PER_IMPORT = 5000


class TraceIngestError(ValueError):
    """trace 导入业务校验失败（API 层映射 400）"""


def ingest_trace_payload(
    db,
    payload: Any,
    *,
    name: str = "",
    description: str = "",
    real_dataset_id: Optional[str] = None,
    created_by: Optional[str] = None,
    team_code: Optional[str] = None,
) -> Dict[str, Any]:
    """trace 载荷 → agent 会话数据集（新建或追加）

    Returns:
        {"dataset_id", "count"（导入 trace 行数）, "format"（otlp|phoenix|
        langfuse）, "traces"（解析出的 trace 总数）, "hub_file_refs"}
    """
    from app.services.otel_trace_parser import parse_trace_payload

    try:
        records, fmt = parse_trace_payload(payload)
    except ValueError as e:
        raise TraceIngestError(str(e))
    if not records:
        raise TraceIngestError("载荷未解析出任何含可读内容的 trace（需有 LLM 消息或带属性的 span）")
    if len(records) > MAX_TRACES_PER_IMPORT:
        raise TraceIngestError(
            f"单次导入 trace 数 {len(records)} 超上限 {MAX_TRACES_PER_IMPORT}，请分批提交")
    span_total = sum(
        int((r.get("metadata") or {}).get("span_count") or 0) for r in records)
    if span_total > MAX_SPANS_PER_IMPORT:
        raise TraceIngestError(
            f"单次导入 span 总数 {span_total} 超上限 {MAX_SPANS_PER_IMPORT}，请分批提交")

    from app.services.session_dataset_service import _import_parsed_sessions

    # entry_key 用 trace 会话 ID（langfuse 记录无 span_count，透传 0 不影响限额）
    all_records: List[Tuple[str, str, Dict[str, Any]]] = [
        (f"trace:{r.get('session_id') or idx}", f"{fmt}-traces", r)
        for idx, r in enumerate(records)
    ]
    result = _import_parsed_sessions(
        db, all_records,
        name=name, description=description or "由 OTel/Phoenix/Langfuse trace 导入",
        real_dataset_id=real_dataset_id, created_by=created_by,
        team_code=team_code,
    )
    result.update({"format": fmt, "traces": len(records)})
    logger.info(
        f"[TraceIngest] {fmt} 载荷导入完成: {result['traces']} trace / "
        f"{span_total} span → dataset {result['dataset_id']}（count={result['count']}）")
    return result
