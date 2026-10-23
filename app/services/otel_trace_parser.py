"""OTel / Phoenix trace 解析器（M8 QE8-4，OTel 兼容导入）

把 OpenTelemetry OTLP/HTTP JSON 与 Arize Phoenix 导出 JSON 解析为统一的
SessionRecord（与 Claude Code .jsonl / Langfuse 导出同构，见
app/services/session_file_parser.py），供 agent 会话数据集导入——进既有
trace 评测链路（长会话 Agent 评测 / 插件评测 trace 口径 / 轨迹断言都以
数据集行的 session_messages 为事实源）。

支持的三种载荷（detect_trace_payload_format 自动识别）：
- ``otlp``    OTLP/HTTP JSON（``{"resourceSpans": [...]}``）：resource →
              scope → span 三级；span 的 kvlist 属性展平，按 traceId 分组
              成会话；LLM span 的 gen_ai.prompt/completion（或 OpenInference
              的 input.value/output.value、llm.input/output_messages）转
              user/assistant 消息，其余 span 转 tool_use/tool_result 对
              （工具序列是轨迹断言的输入）；
- ``phoenix`` Phoenix trace 导出（CLI/UI 下载：单 trace ``{"traceId",
              "spans": [...]}``；兼容旧形态 ``{"data": [...]}`` 与 trace
              数组），span 为 OpenInference 平铺属性（context.trace_id /
              start_time ISO / status_code）；
- ``langfuse``Langfuse trace 载荷（单 trace / 数组 / ``{"traces": [...]}``），
              转换复用 session_file_parser 的既有实现。

定位（文档同口径）：本模块是**单向的「tracing 资产 → 评测样本」入口**，
平台不是 tracing 后端——与 Langfuse/Phoenix/Jaeger 等 tracing 平台共存，
不做 span 存储/检索/看板，只把既有 trace 变成可评测的数据集行。

纯函数模块，仅依赖标准库（与 session_file_parser 同约束）。
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# 单文本块截断上限（字符）：OTel 属性可能带整段 prompt/completion，截断保库
# 行尺寸可控；渲染层（render_session_markdown）另有 4000 字符/块展示截断
_TEXT_BLOCK_LIMIT = 20_000
# tool_use 块的属性入参快照：最多键数与单值字符数（属性值可能是大 JSON）
_TOOL_ATTR_MAX_KEYS = 8
_TOOL_ATTR_VALUE_LIMIT = 500

# LLM 语义属性 → 消息角色的取值键（先 OTel GenAI 语义约定，后 OpenInference）
_LLM_INPUT_KEYS = ("gen_ai.prompt", "input.value")
_LLM_OUTPUT_KEYS = ("gen_ai.completion", "output.value")
_LLM_MESSAGE_LIST_KEYS = ("llm.input_messages", "llm.output_messages")


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

def _truncate_text(value: str, limit: int = _TEXT_BLOCK_LIMIT) -> str:
    """超限截断并标注原长度（与 session_file_parser._truncate_chars 同口径）"""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[已截断，原长度 {len(value)} 字符]"


def _ns_to_iso(value: Any) -> Optional[str]:
    """OTLP UnixNano 时间戳（str/int）→ ISO8601 UTC；非法返回 None"""
    try:
        nanos = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if nanos <= 0:
        return None
    seconds, remainder = divmod(nanos, 1_000_000_000)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder // 1000).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _sort_key_by_time(item: Tuple[int, Dict[str, Any]]):
    """span 排序键：可解析 start_time 在前（按时间），无时间排最后保序"""
    index, span = item
    raw = span.get("start_time")
    if not isinstance(raw, str) or not raw.strip():
        return (1, 0.0, index)
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return (1, 0.0, index)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (0, parsed.timestamp(), index)


def _compute_stats(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """轮次/消息数/工具调用统计（复用 session_file_parser 同口径实现）"""
    from app.services.session_file_parser import _compute_stats as _stats

    return _stats(messages)


def _time_range(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    from app.services.session_file_parser import _time_range as _range

    return _range(messages)


# ---------------------------------------------------------------------------
# OTLP/HTTP JSON
# ---------------------------------------------------------------------------

def _otlp_value(raw: Any) -> Any:
    """OTLP AnyValue 对象 → Python 值（stringValue/intValue/boolValue/数组/kvlist）"""
    if not isinstance(raw, dict):
        return raw
    if "stringValue" in raw:
        return raw["stringValue"]
    if "intValue" in raw:
        try:
            return int(str(raw["intValue"]))
        except (TypeError, ValueError):
            return None
    if "doubleValue" in raw:
        try:
            return float(raw["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in raw:
        return raw["boolValue"] is True
    if "arrayValue" in raw:
        values = (raw.get("arrayValue") or {}).get("values") or []
        return [_otlp_value(v) for v in values]
    if "kvlistValue" in raw:
        kvlist = (raw.get("kvlistValue") or {}).get("kvList") or []
        return {
            kv.get("key"): _otlp_value(kv.get("value"))
            for kv in kvlist if isinstance(kv, dict) and kv.get("key")
        }
    return None


def _flatten_otlp_attributes(items: Any) -> Dict[str, Any]:
    """OTLP attributes 数组 → 平铺 dict（越界/畸形项跳过）"""
    flat: Dict[str, Any] = {}
    if not isinstance(items, list):
        return flat
    for item in items:
        if isinstance(item, dict) and item.get("key"):
            flat[str(item["key"])] = _otlp_value(item.get("value"))
    return flat


# OTLP span kind 数字枚举（SpanKind）：仅作元信息透传，不参与分支
_OTLP_KIND_NAMES = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _normalize_otlp_span(span: Dict[str, Any], resource_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """OTLP span → 统一 span 视图（attributes 平铺 + ISO 时间 + resource 透传）"""
    kind = span.get("kind")
    if isinstance(kind, int):
        kind = _OTLP_KIND_NAMES.get(kind, str(kind))
    status = ""
    status_obj = span.get("status")
    if isinstance(status_obj, dict):
        code = status_obj.get("code")
        status = {0: "UNSET", 1: "OK", 2: "ERROR"}.get(code, str(code)) if isinstance(code, int) else str(code or "")
    return {
        "trace_id": str(span.get("traceId") or ""),
        "span_id": str(span.get("spanId") or ""),
        "parent_id": str(span.get("parentSpanId") or "") or None,
        "name": str(span.get("name") or ""),
        "kind": str(kind or ""),
        "start_time": _ns_to_iso(span.get("startTimeUnixNano")),
        "end_time": _ns_to_iso(span.get("endTimeUnixNano")),
        "attributes": _flatten_otlp_attributes(span.get("attributes")),
        "status": status,
        # resource 属性（service.name 等）平铺透传，供会话 metadata 归因
        "resource_attributes": resource_attrs,
    }


def parse_otlp_payload(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """OTLP/HTTP JSON → SessionRecord 列表（按 traceId 分组，一 trace 一会话）"""
    spans_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for resource_spans in data.get("resourceSpans") or []:
        if not isinstance(resource_spans, dict):
            continue
        resource_attrs = _flatten_otlp_attributes(resource_spans.get("resource", {}).get("attributes"))
        scope_spans_list = resource_spans.get("scopeSpans") or []
        if isinstance(resource_spans.get("scopeSpans"), dict):
            scope_spans_list = [resource_spans["scopeSpans"]]
        for scope_spans in scope_spans_list:
            if not isinstance(scope_spans, dict):
                continue
            for span in scope_spans.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                normalized = _normalize_otlp_span(span, resource_attrs)
                trace_id = normalized["trace_id"] or "_unknown"
                spans_by_trace.setdefault(trace_id, []).append(normalized)

    records = []
    for trace_id, spans in spans_by_trace.items():
        record = _record_from_spans(trace_id, spans, source="otel")
        if record is not None:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Phoenix / OpenInference 导出
# ---------------------------------------------------------------------------

def _normalize_phoenix_span(span: Dict[str, Any]) -> Dict[str, Any]:
    """Phoenix/OpenInference span → 统一 span 视图（平铺属性 + ISO 时间）"""
    context = span.get("context") if isinstance(span.get("context"), dict) else {}
    attributes = span.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    return {
        "trace_id": str(context.get("trace_id") or span.get("trace_id") or ""),
        "span_id": str(context.get("span_id") or span.get("span_id") or ""),
        "parent_id": span.get("parent_id") or None,
        "name": str(span.get("name") or ""),
        "kind": str(span.get("span_kind") or ""),
        "start_time": span.get("start_time"),
        "end_time": span.get("end_time"),
        "attributes": dict(attributes),
        "status": str(span.get("status_code") or ""),
        "resource_attributes": {},
    }


def _iter_phoenix_traces(data: Any) -> List[List[Dict[str, Any]]]:
    """Phoenix 载荷形态归一 → [按 trace 分组的 span 列表]

    兼容三种形态：单 trace（traceId + spans）、{"data": [trace|span...]}、
    trace 对象数组；裸 span（context.trace_id）按 trace_id 二次分组。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []

    def _take_trace_object(obj: Dict[str, Any]) -> None:
        spans = obj.get("spans")
        if isinstance(spans, list) and spans:
            trace_id = str(obj.get("traceId") or obj.get("trace_id")
                           or (spans[0].get("context") or {}).get("trace_id") or "_unknown")
            for span in spans:
                if isinstance(span, dict):
                    _take_span(_normalize_phoenix_span(span), trace_id)
            return
        # 裸 span：自身带 context.trace_id
        _take_span(_normalize_phoenix_span(obj), None)

    def _take_span(normalized: Dict[str, Any], trace_id: Optional[str]) -> None:
        key = trace_id or normalized["trace_id"] or "_unknown"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(normalized)

    if isinstance(data, dict):
        if isinstance(data.get("spans"), list):
            _take_trace_object(data)
        elif isinstance(data.get("data"), list):
            for item in data["data"]:
                if isinstance(item, dict):
                    _take_trace_object(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _take_trace_object(item)
    return [groups[key] for key in order]


def parse_phoenix_payload(data: Any) -> List[Dict[str, Any]]:
    """Phoenix 导出 JSON → SessionRecord 列表（一 trace 一会话）"""
    records = []
    for spans in _iter_phoenix_traces(data):
        trace_id = spans[0]["trace_id"] if spans else ""
        record = _record_from_spans(trace_id or "_unknown", spans, source="phoenix")
        if record is not None:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# 统一 span → SessionRecord（OTLP / Phoenix 共用）
# ---------------------------------------------------------------------------

def _messages_from_llm_attributes(span: Dict[str, Any]) -> List[Dict[str, Any]]:
    """LLM 语义属性 → user/assistant 消息（无 LLM 内容返回空，由调用方走工具分支）

    取值优先级：llm.input/output_messages（结构化消息列表，JSON 字符串或
    原生数组）> gen_ai.prompt/completion（OTel GenAI 语义约定）>
    input.value/output.value（OpenInference 兜底）。
    """
    attrs = span["attributes"]
    messages: List[Dict[str, Any]] = []

    for list_key, default_role, timestamp in (
        ("llm.input_messages", "user", span.get("start_time")),
        ("llm.output_messages", "assistant", span.get("end_time")),
    ):
        raw = attrs.get(list_key)
        items: Any = None
        if isinstance(raw, str) and raw.strip():
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = None
        elif isinstance(raw, list):
            items = raw
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            text = content if isinstance(content, str) and content.strip() else ""
            if not text:
                continue
            role = str(item.get("role") or default_role)
            if role not in ("user", "assistant"):
                role = default_role
            messages.append({
                "role": role,
                "content": [{"type": "text", "text": _truncate_text(text)}],
                "timestamp": timestamp,
            })

    if messages:
        return messages

    def _first_text(keys: Tuple[str, ...]) -> Optional[str]:
        for key in keys:
            value = attrs.get(key)
            if isinstance(value, str) and value.strip():
                return value
            # 数组形态（OpenInference 消息分片 llm.input_messages.0.content 已在
            # 上层列表处理；这里兜底平铺键的字符串值）
        return None

    input_text = _first_text(_LLM_INPUT_KEYS)
    output_text = _first_text(_LLM_OUTPUT_KEYS)
    if input_text:
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": _truncate_text(input_text)}],
            "timestamp": span.get("start_time"),
        })
    if output_text:
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": _truncate_text(output_text)}],
            "timestamp": span.get("end_time"),
        })
    return messages


def _tool_attr_snapshot(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """tool_use 入参快照：字符串值属性截断，最多 N 键（防大 JSON 灌库）"""
    snapshot: Dict[str, Any] = {}
    for key, value in attrs.items():
        if len(snapshot) >= _TOOL_ATTR_MAX_KEYS:
            break
        if isinstance(value, str):
            snapshot[str(key)] = _truncate_text(value, _TOOL_ATTR_VALUE_LIMIT)
        elif isinstance(value, (int, float, bool)):
            snapshot[str(key)] = value
    return snapshot


def _messages_from_tool_span(span: Dict[str, Any]) -> List[Dict[str, Any]]:
    """非 LLM span → tool_use（assistant）+ tool_result（user）消息对

    工具调用序列是轨迹断言（trajectory_matcher）的输入；错误态 span 的
    tool_result 带 is_error=True（失败工具回放可辨）。
    """
    name = span["name"] or span["kind"] or "span"
    snapshot = _tool_attr_snapshot(span["attributes"])
    result_content = json.dumps(
        {"status": span["status"] or None, "attributes": snapshot},
        ensure_ascii=False, default=str)
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": snapshot}],
            "timestamp": span.get("start_time"),
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "content": _truncate_text(result_content, _TOOL_ATTR_VALUE_LIMIT * 4),
                "is_error": span["status"] == "ERROR",
            }],
            "timestamp": span.get("end_time") or span.get("start_time"),
        },
    ]


def _record_from_spans(
    trace_id: str, spans: List[Dict[str, Any]], source: str
) -> Optional[Dict[str, Any]]:
    """一组同 trace span → SessionRecord（无可读内容的 trace 返回 None）"""
    ordered = [span for _, span in sorted(enumerate(spans), key=_sort_key_by_time)]
    messages: List[Dict[str, Any]] = []
    for span in ordered:
        llm_messages = _messages_from_llm_attributes(span)
        if llm_messages:
            messages.extend(llm_messages)
            continue
        # 无 LLM 语义属性：有属性的 span 视为工具/执行 span（属性全空的
        # 框架 span 跳过，避免把链路追踪噪声灌进会话）
        if span["attributes"] or span["status"] == "ERROR":
            messages.extend(_messages_from_tool_span(span))

    if not messages:
        return None

    metadata: Dict[str, Any] = {"span_count": len(ordered)}
    resource_attrs = next(
        (s["resource_attributes"] for s in ordered if s["resource_attributes"]), {})
    for key in ("service.name", "service.version", "deployment.environment.name"):
        if resource_attrs.get(key) is not None:
            metadata[key] = resource_attrs[key]
    span_names = [s["name"] for s in ordered if s["name"]]
    if span_names:
        metadata["span_names"] = span_names[:50]

    started_at, ended_at = _time_range(messages)
    return {
        "session_id": trace_id,
        "source": source,
        "started_at": started_at,
        "ended_at": ended_at,
        "metadata": metadata,
        "messages": messages,
        "stats": _compute_stats(messages),
    }


# ---------------------------------------------------------------------------
# 格式识别与分发入口
# ---------------------------------------------------------------------------

def detect_trace_payload_format(data: Any) -> Optional[str]:
    """载荷形态识别：otlp / phoenix / langfuse；不认识返回 None"""
    if not isinstance(data, dict):
        if isinstance(data, list) and data and isinstance(data[0], dict):
            first = data[0]
            if "spans" in first or "context" in first or "span_kind" in first:
                return "phoenix"
            if "generations" in first or "observations" in first:
                return "langfuse"
        return None
    if isinstance(data.get("resourceSpans"), list):
        return "otlp"
    if isinstance(data.get("spans"), list) or isinstance(data.get("data"), list):
        return "phoenix"
    if isinstance(data.get("traces"), list) or "generations" in data or "observations" in data:
        return "langfuse"
    return None


def parse_trace_payload(data: Any) -> Tuple[List[Dict[str, Any]], str]:
    """统一入口：载荷 → (SessionRecord 列表, 格式名)；不认识抛 ValueError"""
    fmt = detect_trace_payload_format(data)
    if fmt == "otlp":
        return parse_otlp_payload(data), fmt
    if fmt == "phoenix":
        return parse_phoenix_payload(data), fmt
    if fmt == "langfuse":
        from app.services.session_file_parser import parse_langfuse_payload

        return parse_langfuse_payload(data), fmt
    raise ValueError(
        "无法识别的 trace 载荷形态（支持 OTLP/HTTP JSON（resourceSpans）、"
        "Phoenix 导出（traceId+spans / data 数组）、Langfuse 导出（traces 数组））")
