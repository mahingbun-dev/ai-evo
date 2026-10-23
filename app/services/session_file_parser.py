"""
会话文件解析器

将 Claude Code 导出的 .jsonl 会话与 Langfuse 导出的 .json trace 解析为
统一的 SessionRecord（dict）结构，供裁判（judge）流程渲染与评分使用。

约束：
- 纯函数模块，仅依赖标准库，不依赖数据库/框架
- .jsonl 逐行流式读取（文件可能几十 MB），不一次性加载
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# langfuse input/output 为非文本 JSON 时序列化后的最大字符数
_LANGFUSE_TEXT_LIMIT = 2000


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[datetime]:
    """解析 ISO8601 时间字符串；naive 时间统一视为 UTC，解析失败返回 None"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    candidate = text
    if text.endswith("Z"):
        candidate = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_timestamps(messages: List[Dict[str, Any]]) -> List[datetime]:
    """收集消息中可解析的时间戳"""
    stamps = []
    for msg in messages:
        parsed = _parse_ts(msg.get("timestamp"))
        if parsed is not None:
            stamps.append(parsed)
    return stamps


def _compute_stats(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计轮次/消息数/工具调用数/会话时长"""
    turns = 0
    tool_call_count = 0
    for msg in messages:
        blocks = msg.get("content") or []
        if not isinstance(blocks, list):
            blocks = []
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
        )
        if msg.get("role") == "user" and not has_tool_result:
            turns += 1
        tool_call_count += sum(
            1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
        )

    stamps = _message_timestamps(messages)
    duration = (max(stamps) - min(stamps)).total_seconds() if stamps else None
    return {
        "turns": turns,
        "message_count": len(messages),
        "tool_call_count": tool_call_count,
        "duration_seconds": duration,
    }


def _time_range(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """返回会话首末时间的 ISO 字符串；无有效时间返回 (None, None)"""
    stamps = _message_timestamps(messages)
    if not stamps:
        return None, None
    return min(stamps).isoformat(), max(stamps).isoformat()


def _first_non_empty(rows: List[Dict[str, Any]], key: str) -> Optional[Any]:
    """从多行记录中取第一个非空字段值（用于 cwd/version/gitBranch 等）"""
    for row in rows:
        value = row.get(key)
        if value:
            return value
    return None


def _is_tool_result_message(message: Dict[str, Any]) -> bool:
    """判断消息是否为工具结果回传（user 角色携带 tool_result 块）"""
    if message.get("role") != "user":
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in (message.get("content") or [])
    )


# ---------------------------------------------------------------------------
# Claude Code .jsonl 解析
# ---------------------------------------------------------------------------

def _normalize_blocks(content: Any) -> List[Dict[str, Any]]:
    """将 claude message.content 规范化为 ContentBlock 列表，保留原始字段"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": json.dumps(content, ensure_ascii=False, default=str)}]

    blocks = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if not block_type:
            continue
        if block_type == "tool_result":
            # 工具结果以 user 角色回传，is_error 缺省为 False
            block = dict(item)
            block.setdefault("is_error", False)
            blocks.append(block)
        else:
            blocks.append(dict(item))
    return blocks


def parse_claude_code_jsonl(path: str, include_sidechain: bool = False) -> Dict[str, Any]:
    """
    解析 Claude Code 会话导出的 .jsonl 文件

    Args:
        path: .jsonl 文件路径
        include_sidechain: 是否包含子代理（isSidechain）消息，默认排除

    Returns:
        SessionRecord dict

    Raises:
        ValueError: 有效消息为 0 时（含文件名与坏行数）
    """
    file_path = Path(path)
    bad_lines = 0
    session_counter: Counter = Counter()
    session_first_seen: List[str] = []
    metadata_rows: List[Dict[str, Any]] = []
    summary_text: Optional[str] = None
    # (sessionId, is_sidechain, message) 三元组，主会话确定后再过滤
    candidates: List[Tuple[str, bool, Dict[str, Any]]] = []

    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                # 空行静默跳过（常见文件尾）
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(row, dict):
                bad_lines += 1
                continue

            session_id = row.get("sessionId")
            if isinstance(session_id, str) and session_id:
                if session_id not in session_counter:
                    session_first_seen.append(session_id)
                session_counter[session_id] += 1

            # summary 行：写入 metadata["summary"]
            if row.get("type") == "summary" or (
                "summary" in row and row.get("leafUuid")
            ):
                if isinstance(row.get("summary"), str) and row["summary"]:
                    summary_text = row["summary"]
                continue

            # system 行不产生消息
            if row.get("type") == "system":
                continue

            # isMeta 行（如 command 包装消息）跳过
            if row.get("isMeta") is True:
                continue

            role = None
            if row.get("type") in ("user", "assistant"):
                role = row["type"]
            else:
                inner = row.get("message")
                if isinstance(inner, dict) and inner.get("role") in ("user", "assistant"):
                    role = inner["role"]
            if role is None:
                continue

            is_sidechain = row.get("isSidechain") is True
            message_obj = row.get("message")
            if not isinstance(message_obj, dict):
                continue

            blocks = _normalize_blocks(message_obj.get("content"))
            message = {
                "role": role,
                "content": blocks,
                "timestamp": row.get("timestamp"),
            }
            candidates.append((session_id if isinstance(session_id, str) else "", is_sidechain, message))
            metadata_rows.append(row)

    if not session_counter:
        # 没有任何带 sessionId 的有效行
        if not candidates:
            raise ValueError(
                f"会话文件无有效消息: {file_path.name}（坏行数: {bad_lines}）"
            )
        main_session_id = ""
    else:
        # 以出现最多的 sessionId 为主会话；并列时取先出现者
        main_session_id = max(session_first_seen, key=lambda sid: session_counter[sid])

    messages: List[Dict[str, Any]] = []
    dropped_session_ids: List[str] = []
    models = []
    for session_id, is_sidechain, message in candidates:
        if session_id != main_session_id:
            if session_id and session_id not in dropped_session_ids:
                dropped_session_ids.append(session_id)
            continue
        if is_sidechain and not include_sidechain:
            continue
        messages.append(message)

    for row in metadata_rows:
        model = row.get("message")
        if isinstance(model, dict) and model.get("model") and model["model"] not in models:
            models.append(model["model"])

    if not messages:
        raise ValueError(
            f"会话文件无有效消息: {file_path.name}（坏行数: {bad_lines}）"
        )

    metadata: Dict[str, Any] = {
        "cwd": _first_non_empty(metadata_rows, "cwd"),
        "version": _first_non_empty(metadata_rows, "version"),
        "gitBranch": _first_non_empty(metadata_rows, "gitBranch"),
    }
    if summary_text:
        metadata["summary"] = summary_text
    if models:
        metadata["models"] = models
    if dropped_session_ids:
        metadata["dropped_session_ids"] = dropped_session_ids
    if bad_lines > 0:
        metadata["skipped_lines"] = bad_lines

    started_at, ended_at = _time_range(messages)
    return {
        "session_id": main_session_id,
        "source": "claude_code",
        "started_at": started_at,
        "ended_at": ended_at,
        "metadata": metadata,
        "messages": messages,
        "stats": _compute_stats(messages),
    }


# ---------------------------------------------------------------------------
# Langfuse trace 解析
# ---------------------------------------------------------------------------

def _truncate_chars(text: str, limit: int) -> str:
    """超过 limit 字符时截断并标注原长度"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，原长度 {len(text)} 字符]"


def _blocks_from_langfuse_content(content: Any) -> List[Dict[str, Any]]:
    """将 langfuse message.content（str 或 block 列表）转为 ContentBlock 列表"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return _normalize_blocks(content)
    return [
        {
            "type": "text",
            "text": _truncate_chars(
                json.dumps(content, ensure_ascii=False, default=str),
                _LANGFUSE_TEXT_LIMIT,
            ),
        }
    ]


def _message_from_langfuse_value(
    value: Any, role: str, timestamp: Any
) -> Optional[Dict[str, Any]]:
    """
    将 generation 的 input/output 转为对应角色的 Message：

    - {"messages": [...]}: 逐条转换 role/content
    - 纯文本: 单条 text 消息
    - 其他 JSON: 序列化为 text（截断到 2000 字符）
    """
    blocks: List[Dict[str, Any]] = []
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        for msg in value["messages"]:
            if not isinstance(msg, dict):
                continue
            blocks.extend(_blocks_from_langfuse_content(msg.get("content")))
    elif isinstance(value, str):
        if value:
            blocks = [{"type": "text", "text": value}]
    elif value is not None:
        blocks = [
            {
                "type": "text",
                "text": _truncate_chars(
                    json.dumps(value, ensure_ascii=False, default=str),
                    _LANGFUSE_TEXT_LIMIT,
                ),
            }
        ]

    if not blocks:
        return None
    return {"role": role, "content": blocks, "timestamp": timestamp}


def _messages_from_generation(generation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单个 generation 转 2 条 Message（input→user，output→assistant）"""
    if not isinstance(generation, dict):
        return []
    start_time = generation.get("startTime")
    end_time = generation.get("endTime") or start_time
    messages = []
    user_message = _message_from_langfuse_value(generation.get("input"), "user", start_time)
    if user_message:
        messages.append(user_message)
    assistant_message = _message_from_langfuse_value(
        generation.get("output"), "assistant", end_time
    )
    if assistant_message:
        messages.append(assistant_message)
    return messages


def _generation_meta(generation: Dict[str, Any]) -> Dict[str, Any]:
    """提取 generation 的 model/startTime 等元信息"""
    keys = (
        "id",
        "name",
        "model",
        "modelParameters",
        "startTime",
        "endTime",
        "usage",
        "level",
        "statusMessage",
    )
    return {key: generation[key] for key in keys if key in generation}


def _extract_langfuse_messages(
    trace: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    按顺序尝试消息提取策略，返回 (messages, generation_meta_list)：

    1. trace.generations 数组
    2. trace.observations 中 type == "GENERATION" 的项
    3. 顶层 input/output 直接含 messages
    取第一个能产出消息的策略。
    """
    # 策略 1: generations
    generations = trace.get("generations")
    if isinstance(generations, list) and generations:
        messages = []
        gen_meta = []
        for generation in generations:
            messages.extend(_messages_from_generation(generation))
            gen_meta.append(_generation_meta(generation))
        if messages:
            return messages, gen_meta

    # 策略 2: observations 中 GENERATION 类型
    observations = trace.get("observations")
    if isinstance(observations, list):
        gen_items = [
            item
            for item in observations
            if isinstance(item, dict) and item.get("type") == "GENERATION"
        ]
        if gen_items:
            messages = []
            gen_meta = []
            for generation in gen_items:
                messages.extend(_messages_from_generation(generation))
                gen_meta.append(_generation_meta(generation))
            if messages:
                return messages, gen_meta

    # 策略 3: 顶层 input/output
    messages = []
    if trace.get("input") is not None:
        user_message = _message_from_langfuse_value(trace.get("input"), "user", None)
        if user_message:
            messages.append(user_message)
    if trace.get("output") is not None:
        assistant_message = _message_from_langfuse_value(
            trace.get("output"), "assistant", None
        )
        if assistant_message:
            messages.append(assistant_message)
    return messages, []


def _sort_messages_by_timestamp(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按时间戳稳定排序（无时间戳的排在最后，保持原有相对顺序）"""
    indexed = list(enumerate(messages))

    def sort_key(pair):
        index, message = pair
        parsed = _parse_ts(message.get("timestamp"))
        # 无时间的排最后：用 (1, index)；有时间的用 (0, 时间)
        if parsed is None:
            return (1, 0, index)
        return (0, parsed.timestamp(), index)

    return [message for _, message in sorted(indexed, key=sort_key)]


def _normalize_traces(data: Any) -> List[Dict[str, Any]]:
    """归一化输入形态：单 trace / trace 数组 / {"traces": [...]} 包装"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        traces = data.get("traces")
        if isinstance(traces, list):
            return [item for item in traces if isinstance(item, dict)]
        return [data]
    return []


def _record_from_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    """单个 langfuse trace 转 SessionRecord"""
    messages, generation_meta = _extract_langfuse_messages(trace)
    messages = _sort_messages_by_timestamp(messages)

    metadata: Dict[str, Any] = {}
    for key in ("name", "tags", "userId", "sessionId"):
        if trace.get(key) is not None:
            metadata[key] = trace[key]
    if generation_meta:
        metadata["generations"] = generation_meta

    started_at, ended_at = _time_range(messages)
    return {
        "session_id": str(trace.get("id") or ""),
        "source": "langfuse",
        "started_at": started_at,
        "ended_at": ended_at,
        "metadata": metadata,
        "messages": messages,
        "stats": _compute_stats(messages),
    }


def parse_langfuse_trace(path: str) -> List[Dict[str, Any]]:
    """
    解析 Langfuse 官方导出 JSON（单 trace / trace 数组 / {"traces": [...]} 包装）

    Args:
        path: .json 文件路径

    Returns:
        SessionRecord 列表（保持输入顺序；宽容解析，无效项跳过）
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return [_record_from_trace(trace) for trace in _normalize_traces(data)]


def parse_langfuse_payload(data: Any) -> List[Dict[str, Any]]:
    """内存态 Langfuse trace 载荷 → SessionRecord 列表（M8 QE8-4 OTel 兼容导入）

    与 parse_langfuse_trace 同一归一化/转换路径（单 trace / 数组 /
    ``{"traces": [...]}`` 包装），供 /ingest/otel-traces 直接吃 API 推送
    的 JSON 体（不经临时文件）。
    """
    return [_record_from_trace(trace) for trace in _normalize_traces(data)]


# ---------------------------------------------------------------------------
# 分发入口
# ---------------------------------------------------------------------------

def detect_and_parse(path: str) -> List[Dict[str, Any]]:
    """
    按扩展名分发解析

    - .jsonl → Claude Code 会话（单元素列表）
    - .json  → Langfuse trace（可能多条）
    - 其他扩展名抛 ValueError
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".jsonl":
        return [parse_claude_code_jsonl(path)]
    if suffix == ".json":
        return parse_langfuse_trace(path)
    raise ValueError(f"不支持的会话文件类型: {suffix or '(无扩展名)'}（支持 .jsonl / .json）")


# ---------------------------------------------------------------------------
# 渲染与摘要
# ---------------------------------------------------------------------------

def _render_tool_result_content(content: Any) -> str:
    """渲染 tool_result 的 content：list 逐项渲染，dict 序列化为 JSON"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False, indent=2, default=str))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return "\n\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=2, default=str)
    return str(content)


def render_session_markdown(
    session: Dict[str, Any], max_block_chars: int = 4000
) -> str:
    """
    将 SessionRecord 渲染为裁判可读的 markdown

    - 单个内容块超过 max_block_chars 时截断（追加原长度标注）
    - thinking 块渲染为 "💭 思考：" 引用块
    - 序号对 user/assistant 消息连续递增
    """
    source_label = "Claude Code" if session.get("source") == "claude_code" else "Langfuse"
    stats = session.get("stats") or {}
    started_at = session.get("started_at")
    ended_at = session.get("ended_at")
    time_text = (
        f"{started_at} ~ {ended_at}" if (started_at or ended_at) else "未知"
    )

    lines = [
        f"# 会话记录（来源：{source_label}）",
        "",
        f"- 会话 ID: {session.get('session_id', '')}",
        f"- 时间: {time_text}",
        f"- 轮次: {stats.get('turns', 0)}，消息数: {stats.get('message_count', 0)}，"
        f"工具调用: {stats.get('tool_call_count', 0)}",
        "",
    ]

    for index, message in enumerate(session.get("messages") or [], start=1):
        role = message.get("role")
        if role == "user":
            lines.append(f"## [{index}] 👤 用户")
        else:
            lines.append(f"## [{index}] 🤖 助手")
        lines.append("")

        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
                text = block.get("text") or ""
                if text:
                    lines.append(_truncate_chars(text, max_block_chars))
                    lines.append("")

            elif block_type == "thinking":
                thinking = _truncate_chars(block.get("thinking") or "", max_block_chars)
                quoted = "\n".join(
                    f"> {row}" for row in thinking.splitlines() or [""]
                )
                lines.append("> 💭 思考：")
                lines.append(quoted)
                lines.append("")

            elif block_type == "tool_use":
                tool_name = block.get("name") or "unknown"
                input_json = json.dumps(
                    block.get("input") or {}, ensure_ascii=False, indent=2, default=str
                )
                lines.append(f"### 🔧 工具调用: {tool_name}")
                lines.append("")
                lines.append("```json")
                lines.append(_truncate_chars(input_json, max_block_chars))
                lines.append("```")
                lines.append("")

            elif block_type == "tool_result":
                is_error = block.get("is_error") is True
                header = "### 📋 工具结果 ❌ 错误" if is_error else "### 📋 工具结果"
                rendered = _render_tool_result_content(block.get("content"))
                lines.append(header)
                lines.append("")
                if rendered:
                    lines.append(_truncate_chars(rendered, max_block_chars))
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
_COMMAND_BLOCK_RE = re.compile(r"<command-[a-z-]+>[\s\S]*?</command-[a-z-]+>")


def _humanize_user_text(text: str) -> str:
    """用户消息文本转可读摘要

    Claude Code 斜杠命令消息是 XML 包装（<command-message>/<command-name>/
    <command-args>），直接展示为 <command-message>anc... 不可读；
    转换为「/命令名 参数 正文」形式。
    """
    text = text.strip()
    if not text:
        return ""
    name_match = _COMMAND_NAME_RE.search(text)
    if not name_match:
        return " ".join(text.split())
    command = "/" + name_match.group(1).strip().lstrip("/")
    parts = [command]
    args_match = _COMMAND_ARGS_RE.search(text)
    if args_match:
        args = " ".join(args_match.group(1).split())
        if args:
            parts.append(args)
    # 命令包装标签之外可能还带用户正文
    remainder = _COMMAND_BLOCK_RE.sub(" ", text)
    remainder = re.sub(r"<[^>]+>", " ", remainder)
    remainder = " ".join(remainder.split())
    if remainder:
        parts.append(remainder)
    return " ".join(parts)


def summarize_first_user_message(
    session: Dict[str, Any], limit: int = 80
) -> str:
    """
    第一条真实用户消息（非 tool_result 回传）的 text 拼接摘要

    斜杠命令消息（XML 包装）转换为「/命令名 参数」可读形式；
    超过 limit 字符时截断并追加 "..."；无真实用户消息返回空字符串
    """
    for message in session.get("messages") or []:
        if _is_tool_result_message(message):
            continue
        if message.get("role") != "user":
            continue
        texts = [
            block.get("text")
            for block in (message.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        if texts:
            joined = _humanize_user_text("\n".join(texts))
            if not joined:
                continue
            if len(joined) > limit:
                return joined[:limit] + "..."
            return joined
    return ""
