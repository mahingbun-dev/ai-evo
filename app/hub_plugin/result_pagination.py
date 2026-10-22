"""task_execution_detail 的 items 分页切片后处理。

指定执行版本的全量 result 可能几百 KB～几 MB（每题模型输出 + 裁判完整评语），
而 anc-hub 信封内联上限 4MB、工具结果会整体进入调用方 agent 的上下文。
本模块在插件侧把明细数组切成页（后端 /history/{execution_number} 返回单
JSONB 大列，插件整列拉回后切片，只把当页写进回包信封）：

- 明细数组定位与 result_report 同口径：items+failed_items（judge/plugin，
  合并后按 idx 排序）→ detail_results（rag）→ records（latency）；
- 当页长文本不截断（页大小控制总量），其余顶层键（汇总标量/dimension_scores）原样保留；
- 无明细数组（failed 版本 error 摘要等小载荷）原样返回。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 分页默认值与上限（manifest.py 里 task_execution_detail 的 desc 与本值同口径）
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

# result 里的明细数组键（与 app/services/result_report.py 的 _DETAIL_ARRAY_KEYS 同口径）
_DETAIL_ARRAY_KEYS = ("items", "failed_items", "detail_results", "records")

# idx 缺失时的排序哨兵：排在有 idx 的行之后，保持稳定序
_IDX_SENTINEL = 10 ** 9

NOTE_TEXT = (
    "明细已按 skip/limit 切页（默认 10 题/页，上限 50），当页长文本未截断；"
    "翻页继续调本工具，全量归档请用 task_execution_export 导出 markdown/json 文档"
)


def paginate_detail(data: Any, ctx: Dict[str, Any]) -> Any:
    """task_execution_detail 的响应后处理：明细数组切片 + 分页元信息。"""
    payload = (ctx.get("payload") or {}) if isinstance(ctx, dict) else {}
    skip = _as_int(payload.get("skip"), 0)
    limit = _as_int(payload.get("limit"), DEFAULT_PAGE_SIZE)
    skip = max(0, skip)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        return data
    result = data["result"]

    rows = _collect_rows(result)
    if rows is None:
        return data

    total = len(rows)
    page = rows[skip:skip + limit]
    for key in _DETAIL_ARRAY_KEYS:
        result.pop(key, None)
    result["rows"] = page
    result["rows_page"] = {"skip": skip, "limit": limit, "returned": len(page), "total": total}
    result["note"] = NOTE_TEXT
    return data


def _collect_rows(result: Dict[str, Any]) -> List[Dict[str, Any]] | None:
    """定位明细数组；没有明细数组时返回 None（小载荷，调用方原样返回）。"""
    items = result.get("items")
    if isinstance(items, list) and items:
        rows = [r for r in list(items) + list(result.get("failed_items") or []) if isinstance(r, dict)]
        rows.sort(key=lambda r: r["idx"] if isinstance(r.get("idx"), int) else _IDX_SENTINEL)
        return rows
    for key in ("detail_results", "records"):
        value = result.get(key)
        if isinstance(value, list) and value:
            return [r for r in value if isinstance(r, dict)]
    return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
