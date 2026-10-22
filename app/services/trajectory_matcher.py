"""轨迹匹配器（M2 QE2-2，确定性轨迹断言）

泛化 plugin_eval_trace 的经验（docs 见 app/services/plugin_eval_trace.py 模块
头：数据集 ``session_messages`` 是尺寸控制后的会话记录，直接喂裁判会对截断
trace 系统性虚高——确定性轨迹匹配把「工具调用序列对不对」从裁判手里拿回来）：
agent 类型任务的会话来自结构化 SessionRecord（会话文件导入 jsonl/json →
``session_messages``，见 session_dataset_service.build_session_data），本模块
对其 ``messages`` 内的 ``tool_use`` 块按发生顺序提取工具调用序列，做四模式
确定性匹配，输出 ``{matched, missing[], unexpected[], forbidden_hits[]}``。

任务配置（task.trajectory_config，JSON 列；仅 agent 类型生效）::

    {
      "mode": "strict | unordered | subset | superset",
      "expected": [
        {"tool": "grep", "args_contains": ["pattern"], "min_calls": 1},
        {"tool": "Bash", "min_calls": 2}
      ],
      "forbidden": [{"tool": "rm"}, {"tool": "Bash", "args_contains": ["sudo"]}]
    }

四模式语义（顺序敏感 × 期望全出现 × 额外调用三个维度的组合）：

==========  ========  ============  =========================
模式         顺序敏感  期望须全出现   额外（未期望）调用
==========  ========  ============  =========================
strict      是        是            不允许（unexpected 判失败）
unordered   否        是            允许（unexpected 仅记录）
subset      是(子序列) 是            允许（unexpected 仅记录）
superset    否        否             不允许（unexpected 判失败）
==========  ========  ============  =========================

- ``expected`` 条目：``tool``（精确名匹配）+ 可选 ``args_contains``（每个
  子串都须出现在调用参数的 JSON 序列化文本里）+ ``min_calls``（缺省 1，
  需要至少 N 次满足该条目的调用）；
- ``forbidden`` 条目：命中任一（tool 相等且 args_contains 全满足）即
  forbidden_hit，任何模式下都判失败（禁用工具不因模式宽松而放过）；
- 行级结果 ``status``：error（会话无结构化消息/配置非法）优先，其次任一
  「判失败维度」命中即 fail，否则 pass；独立于裁判维度计分（不算入裁判
  均分），任务级聚合 ``trajectory_match_rate``（接入门禁 metric，见
  result_aggregator / gate_service）。

行级落库：evaluation_results.trajectory_match JSONB（投影列，事实源仍是
row_result JSON 内同名字段）；执行器接线见 evaluation_executor._evaluate_single
步骤1.7（agent 类型 + trajectory_config 非空时运行）。
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 匹配模式白名单
TRAJECTORY_MODES = ("strict", "unordered", "subset", "superset")

# evidence 中调用参数片段的截断长度（字符）
ARGS_SNIPPET_LIMIT = 120
# 时间线展示的最大调用数（超出截断，计数保留全量）
TIMELINE_CALL_LIMIT = 500

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"

# 各模式的失败维度：missing=期望未全出现；unexpected=出现未期望调用
_MODE_FAILS_ON = {
    "strict": ("missing", "unexpected"),
    "unordered": ("missing",),
    "subset": ("missing",),
    "superset": ("unexpected",),
}

__all__ = [
    "TRAJECTORY_MODES",
    "extract_tool_calls",
    "match_trajectory",
    "run_trajectory_match",
    "validate_trajectory_shape",
]


# ==================== 工具调用序列提取 ====================

def extract_tool_calls(session_messages: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """从 SessionRecord 提取按发生顺序的工具调用序列

    Args:
        session_messages: 数据项的 session_messages 字段——SessionRecord dict
            （``{messages: [{role, content: blocks, timestamp}], ...}``，会话
            文件导入产物）或直接的 messages 列表；markdown 字符串（手工导入
            的已渲染文本）无结构化块，返回错误说明。

    Returns:
        (calls, error)：calls 为 ``[{tool, args, timestamp}]``；不可提取时
        calls 为空列表、error 为面向配置者的原因文案。
    """
    if isinstance(session_messages, str) or session_messages is None:
        return [], (
            "会话为渲染文本/空（手工导入的 markdown），无结构化 tool_use 块，"
            "轨迹序列不可提取——请用会话文件导入（jsonl/json）的数据集"
        )
    messages = None
    if isinstance(session_messages, list):
        messages = session_messages
    elif isinstance(session_messages, dict):
        raw = session_messages.get("messages")
        if isinstance(raw, list):
            messages = raw
    if messages is None:
        return [], "session_messages 结构不合法（期望 SessionRecord dict 或 messages 列表）"

    calls: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            calls.append({
                "tool": str(block.get("name") or "unknown"),
                "args": block.get("input") if isinstance(block.get("input"), dict) else {},
                "timestamp": message.get("timestamp"),
            })
    return calls, None


def _args_text(args: Dict[str, Any]) -> str:
    """调用参数的匹配用序列化文本（args_contains 在该文本上做子串匹配）"""
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _entry_matches(call: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """单个调用是否满足期望/禁用条目（tool 精确相等 + args_contains 全包含）"""
    if call.get("tool") != entry.get("tool"):
        return False
    for needle in entry.get("args_contains") or []:
        if str(needle) not in _args_text(call.get("args") or {}):
            return False
    return True


# ==================== 四模式匹配 ====================

def match_trajectory(
    calls: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """对工具调用序列做四模式确定性匹配（纯函数，不查库不落盘）

    Args:
        calls: extract_tool_calls 产物（[{tool, args, timestamp}]，按发生顺序）
        config: {mode, expected: [...], forbidden: [...]}

    Returns:
        {mode, status, matched, missing, unexpected, forbidden_hits,
         matched_count, total_expected, total_calls, timeline}
        timeline 为前端时间线展示用（调用 + 每条调用的覆盖分类，截断上限）。
    """
    mode = str(config.get("mode") or "subset").strip().lower() or "subset"
    if mode not in TRAJECTORY_MODES:
        return _error_result(f"非法匹配模式: {mode!r}（可用: {', '.join(TRAJECTORY_MODES)}）")
    expected = _normalize_entries(config.get("expected"))
    forbidden = _normalize_entries(config.get("forbidden"))

    # 禁用工具：任何模式下命中即失败（不因模式宽松而放过）
    forbidden_hits: List[Dict[str, Any]] = []
    for index, call in enumerate(calls):
        for entry in forbidden:
            if _entry_matches(call, entry):
                forbidden_hits.append({
                    "index": index, "tool": call.get("tool"),
                    "args_contains": entry.get("args_contains") or [],
                })
                break

    matched: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    unexpected: List[Dict[str, Any]] = []

    if mode == "strict":
        matched, missing, unexpected = _match_strict(calls, expected)
    elif mode == "unordered":
        matched, missing, unexpected = _match_unordered(calls, expected)
    elif mode == "subset":
        matched, missing, unexpected = _match_subset(calls, expected)
    else:  # superset
        matched, missing, unexpected = _match_superset(calls, expected)

    fails_on = _MODE_FAILS_ON[mode]
    failed = (
        ("missing" in fails_on and missing)
        or ("unexpected" in fails_on and unexpected)
        or forbidden_hits
    )
    return {
        "mode": mode,
        "status": STATUS_FAIL if failed else STATUS_PASS,
        "matched": matched,
        "missing": missing,
        "unexpected": unexpected,
        "forbidden_hits": forbidden_hits,
        "matched_count": len(matched),
        "total_expected": len(expected),
        "total_calls": len(calls),
        "total_forbidden": len(forbidden),
        "timeline": _build_timeline(calls, unexpected, forbidden_hits),
    }


def _match_strict(
    calls: List[Dict[str, Any]], expected: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """strict：实际序列须与期望展开序列（条目 × min_calls）逐位一致

    位置对齐：第 j 个期望位对应第 j 个调用；错位即同时记 missing（该期望位）
    与 unexpected（该调用）。超出期望长度的调用记 unexpected，超出调用长度
    的期望位记 missing。
    """
    requirements = [
        (entry_index, entry) for entry_index, entry in enumerate(expected)
        for _ in range(_entry_min_calls(entry))
    ]
    per_entry_positions: Dict[int, List[int]] = {i: [] for i in range(len(expected))}
    unexpected: List[Dict[str, Any]] = []
    for position in range(max(len(requirements), len(calls))):
        req_entry = requirements[position][1] if position < len(requirements) else None
        call = calls[position] if position < len(calls) else None
        if req_entry is not None and call is not None:
            if _entry_matches(call, req_entry):
                per_entry_positions[requirements[position][0]].append(position)
            else:
                unexpected.append({"index": position, "tool": call.get("tool")})
        elif call is not None:  # 期望耗尽，多余调用
            unexpected.append({"index": position, "tool": call.get("tool")})

    matched, missing = [], []
    for entry_index, entry in enumerate(expected):
        positions = per_entry_positions[entry_index]
        summary = _entry_summary(entry, positions)
        if len(positions) >= _entry_min_calls(entry):
            matched.append(summary)
        else:
            summary["matched_count"] = len(positions)
            missing.append(summary)
    return matched, missing, unexpected


def _match_unordered(
    calls: List[Dict[str, Any]], expected: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """unordered：顺序无关的计数匹配——条目满足次数 ≥ min_calls 即 matched

    unexpected 为记录项（匹配不到任何期望条目的调用），不参与失败判定。
    """
    matched, missing = [], []
    for entry in expected:
        indexes = [i for i, call in enumerate(calls) if _entry_matches(call, entry)]
        summary = _entry_summary(entry, indexes)
        if len(indexes) >= _entry_min_calls(entry):
            matched.append(summary)
        else:
            summary["matched_count"] = len(indexes)
            missing.append(summary)
    unexpected = [
        {"index": i, "tool": call.get("tool")}
        for i, call in enumerate(calls)
        if not any(_entry_matches(call, entry) for entry in expected)
    ]
    return matched, missing, unexpected


def _match_subset(
    calls: List[Dict[str, Any]], expected: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """subset：期望须按序作为子序列出现（条目连续消耗 min_calls 个匹配调用）

    单次前向扫描：指针只前进——后序条目只能匹配更晚的调用（顺序敏感）；
    指针扫尽仍凑不够 min_calls 记 missing。剩余调用不打扰（额外调用允许，
    unexpected 仅记录供展示）。
    """
    matched, missing = [], []
    pointer = 0
    for entry in expected:
        need = _entry_min_calls(entry)
        found: List[int] = []
        while pointer < len(calls) and len(found) < need:
            if _entry_matches(calls[pointer], entry):
                found.append(pointer)
            pointer += 1
        # 回退指针到「本条目最后一个匹配调用」之后（未匹配的中间调用可被后序条目消费）
        if found:
            pointer = found[-1] + 1
        summary = _entry_summary(entry, found)
        if len(found) >= need:
            matched.append(summary)
        else:
            summary["matched_count"] = len(found)
            missing.append(summary)
    unexpected = [
        {"index": i, "tool": call.get("tool")}
        for i, call in enumerate(calls)
        if not any(_entry_matches(call, entry) for entry in expected)
    ]
    return matched, missing, unexpected


def _match_superset(
    calls: List[Dict[str, Any]], expected: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """superset：实际调用必须是期望的子集——每条调用都要被某期望条目覆盖

    期望条目不要求全出现（missing 恒空，条目计数仅记录在 matched 供展示）；
    出现未被覆盖的调用即 unexpected（判失败维度）。
    """
    matched, missing = [], []
    for entry in expected:
        indexes = [i for i, call in enumerate(calls) if _entry_matches(call, entry)]
        matched.append(_entry_summary(entry, indexes))
    unexpected = [
        {"index": i, "tool": call.get("tool")}
        for i, call in enumerate(calls)
        if not any(_entry_matches(call, entry) for entry in expected)
    ]
    return matched, missing, unexpected


# ==================== 执行入口（executor 步骤1.7 调用） ====================

def run_trajectory_match(
    task,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """执行任务配置的轨迹匹配（agent 类型，M2 QE2-2 步骤1.7）

    Args:
        task: 评测任务对象（读取 trajectory_config / evaluation_type / id）
        payload: 评测请求 payload（_dataset_item.data.session_messages 为轨迹来源）

    Returns:
        匹配结果 dict（含 status/timeline，行级落 trajectory_match 列）；
        任务未配置 trajectory_config 返回 None（result 不写该键，兼容旧结构）。
    """
    config = getattr(task, "trajectory_config", None)
    if not isinstance(config, dict) or not config:
        return None

    payload = payload or {}
    dataset_item = payload.get("_dataset_item") or {}
    if not isinstance(dataset_item, dict):
        dataset_item = {}
    data = dataset_item.get("data") if isinstance(dataset_item.get("data"), dict) else {}
    calls, extract_error = extract_tool_calls(
        data.get("session_messages") or dataset_item.get("session_messages"))
    if extract_error:
        result = _error_result(extract_error)
    else:
        result = match_trajectory(calls, config)
    result["task_id"] = str(getattr(task, "id", "") or "unknown")
    return result


# ==================== 配置形态校验（evaluation_task_service 调用） ====================

def validate_trajectory_shape(config: Any) -> None:
    """轨迹断言配置白名单校验（create/update 前置；非法抛 ValueError → API 400）"""
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("trajectory_config 必须是对象 {mode, expected, forbidden}")

    mode = str(config.get("mode") or "subset").strip().lower() or "subset"
    if mode not in TRAJECTORY_MODES:
        raise ValueError(
            f"trajectory_config.mode 非法: {mode!r}（可用: {', '.join(TRAJECTORY_MODES)}）")

    for key in ("expected", "forbidden"):
        entries = config.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"trajectory_config.{key} 必须是数组")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"trajectory_config.{key} 第 {i} 项必须是对象")
            tool = str(entry.get("tool") or "").strip()
            if not tool:
                raise ValueError(f"trajectory_config.{key} 第 {i} 项缺少 tool")
            args_contains = entry.get("args_contains")
            if args_contains is not None:
                if not isinstance(args_contains, list) or not all(
                    isinstance(a, str) and a for a in args_contains
                ):
                    raise ValueError(
                        f"trajectory_config.{key} 第 {i} 项 args_contains 须为非空字符串数组")
            if key == "expected":
                min_calls = entry.get("min_calls", 1)
                if not isinstance(min_calls, int) or isinstance(min_calls, bool) or min_calls < 1:
                    raise ValueError(
                        f"trajectory_config.expected 第 {i} 项 min_calls 须为 ≥1 整数")


# ==================== 工具 ====================

def _normalize_entries(raw: Any) -> List[Dict[str, Any]]:
    """归一化 expected/forbidden 条目（tool 必填、args_contains 列表化、min_calls 兜底）"""
    if not isinstance(raw, list):
        return []
    entries: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if not tool:
            continue
        entry: Dict[str, Any] = {"tool": tool}
        args_contains = item.get("args_contains")
        if isinstance(args_contains, list) and args_contains:
            entry["args_contains"] = [str(a) for a in args_contains]
        if "min_calls" in item:
            entry["min_calls"] = _entry_min_calls(item)
        entries.append(entry)
    return entries


def _entry_min_calls(entry: Dict[str, Any]) -> int:
    """条目的 min_calls（缺省 1；非法值兜底 1，配置侧已白名单校验）"""
    value = entry.get("min_calls", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _entry_summary(entry: Dict[str, Any], call_indexes: List[int]) -> Dict[str, Any]:
    """matched/missing 条目的展示摘要（条目口径 + 命中的调用序号）"""
    summary: Dict[str, Any] = {
        "tool": entry.get("tool"),
        "min_calls": _entry_min_calls(entry),
        "matched_count": len(call_indexes),
        "call_indexes": call_indexes[:TIMELINE_CALL_LIMIT],
    }
    if entry.get("args_contains"):
        summary["args_contains"] = entry["args_contains"]
    return summary


def _build_timeline(
    calls: List[Dict[str, Any]],
    unexpected: List[Dict[str, Any]],
    forbidden_hits: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """前端时间线：逐调用 {index, tool, args_snippet, kind}

    kind 分类：forbidden（禁用命中）> unexpected（未期望）> covered（被期望
    覆盖）；截断上限 TIMELINE_CALL_LIMIT（计数以 total_calls 为准）。
    """
    unexpected_indexes = {item["index"] for item in unexpected}
    forbidden_indexes = {item["index"] for item in forbidden_hits}
    timeline: List[Dict[str, Any]] = []
    for index, call in enumerate(calls[:TIMELINE_CALL_LIMIT]):
        if index in forbidden_indexes:
            kind = "forbidden"
        elif index in unexpected_indexes:
            kind = "unexpected"
        else:
            kind = "covered"
        args_text = _args_text(call.get("args") or {})
        timeline.append({
            "index": index,
            "tool": call.get("tool"),
            "args_snippet": (
                args_text if len(args_text) <= ARGS_SNIPPET_LIMIT
                else args_text[:ARGS_SNIPPET_LIMIT] + "…"
            ),
            "kind": kind,
            "timestamp": call.get("timestamp"),
        })
    return timeline


def _error_result(message: str) -> Dict[str, Any]:
    """status=error 的整体结果（会话不可提取/配置非法；独立于断言失败）"""
    return {
        "mode": None,
        "status": STATUS_ERROR,
        "matched": [],
        "missing": [],
        "unexpected": [],
        "forbidden_hits": [],
        "matched_count": 0,
        "total_expected": 0,
        "total_calls": 0,
        "total_forbidden": 0,
        "timeline": [],
        "error": message,
    }
