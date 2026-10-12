"""评测结果摘要裁剪。

评测结果里 ``items``（成功项明细）与 ``failed_items``（失败样例明细）可能远超
anc-hub 信封的 4MB 内联上限，且对 agent 决策而言信息密度太低。本模块把
result_aggregator 的全量输出裁成摘要：保留通过率/平均分/维度得分，失败样例只留
前 N 条（长文本字段截断），成功项明细整块省略只留计数。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 摘要阈值（manifest.py 里 TOOL_ROUTES 引用的同款默认值，独立成常量便于测试）
DEFAULT_MAX_FAILED_ITEMS = 10
DEFAULT_MAX_FIELD_CHARS = 300
DEFAULT_MAX_LIST_ITEMS = 3


def summarize_result(
    result: Any,
    max_failed_items: int = DEFAULT_MAX_FAILED_ITEMS,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
) -> Any:
    """把 ai-evo 的 result 明裁成摘要；非 dict（None/异常态）原样返回。"""
    if not isinstance(result, dict):
        return result

    failed_items = result.get("failed_items")
    items = result.get("items")

    summary: Dict[str, Any] = {}
    # 先保留除 items/failed_items 外的所有键（total/success_rate/dimension_scores 等都不大）
    for key, value in result.items():
        if key not in ("items", "failed_items"):
            summary[key] = _truncate_value(value, max_field_chars)

    failed_list = failed_items if isinstance(failed_items, list) else []
    kept_failed = [
        _truncate_value(item, max_field_chars) for item in failed_list[:max_failed_items]
    ]
    summary["failed_items"] = kept_failed
    if len(failed_list) > max_failed_items:
        summary["failed_items_omitted"] = len(failed_list) - max_failed_items

    if isinstance(items, list):
        summary["items_omitted"] = len(items)
        # 成功样例一条不留：agent 关心的是失败原因，成功明细看前端
    return summary


def _truncate_value(value: Any, max_chars: int, depth: int = 0) -> Any:
    """递归裁剪：超长字符串截断、超长列表截断、防环限深。"""
    if depth > 6:
        return "..."
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + f"…（已截断，原文 {len(value)} 字）"
        return value
    if isinstance(value, list):
        kept = [_truncate_value(v, max_chars, depth + 1) for v in value[:DEFAULT_MAX_LIST_ITEMS]]
        if len(value) > DEFAULT_MAX_LIST_ITEMS:
            kept.append(f"…（共 {len(value)} 项，仅保留前 {DEFAULT_MAX_LIST_ITEMS} 项）")
        return kept
    if isinstance(value, dict):
        return {
            str(k): _truncate_value(v, max_chars, depth + 1)
            for k, v in value.items()
        }
    # int/float/bool/None 原样
    return value


def build_result_url(frontend_base: str, path: str = "/results") -> str:
    """前端结果页链接（Results 页内自行选择任务，链接只落到页面）。"""
    return f"{(frontend_base or '').rstrip('/')}{path}"


def summary_note(max_failed_items: int) -> str:
    return (
        f"已省略全部成功项明细与第 {max_failed_items} 条之后的失败样例；"
        "需要全量明细请在前端结果页查看"
    )


def expected_keys_for_test() -> List[str]:
    """测试辅助：摘要里必有的键。"""
    return ["failed_items", "items_omitted"]
