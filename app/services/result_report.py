"""评测执行结果导出文档渲染。

插件 task_execution_export 工具的文档生成侧：把单次执行记录渲染成两种格式——

- ``build_json_export``：json 无损导出，result 原始结构原样保留（loads 后与
  库内 dict 深相等），失败版本同样可导（错误上下文齐全）；
- ``build_markdown_export``：人读全量报告——任务元信息、配置摘要、汇总指标、
  逐题明细（裁判评语全文，不截断）；failed/stopped 版本只出失败简报。

渲染按 result 实际结构自适应（明细数组键：items+failed_items → detail_results
→ records），类型特化仅 judge/plugin 单题模板，其余类型走通用键值渲染，
避免与 result_aggregator 的评测类型清单耦合。

输入 ``record`` 为 TaskExecutionRecord（或同属性鸭子类型），``task`` 为
EvaluationTask（仅需 id/name/evaluation_type）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# judge/plugin 单题条目里的结构键：不进入「输入字段」平铺区
_ITEM_STRUCTURAL_KEYS = frozenset({
    "idx", "success", "model_output", "model_raw_response", "judge_count",
    "judge_scores", "judge_details", "feedback", "dimension_scores",
    "row_status", "failed_judge_count", "average_score", "pre_script_results",
    "input_data", "error", "evaluation_target_name",
})

# 汇总区不重复渲染的明细数组键（明细单列章节）
_DETAIL_ARRAY_KEYS = frozenset({"items", "failed_items", "detail_results", "records"})

# markdown 报告里 judge_details/dimension_scores 等单裁判结构中的子键
_JD_FEEDBACK_KEYS = ("feedback", "dimension_feedbacks")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (value or None)


def build_json_export(record: Any, task: Any) -> bytes:
    """无损 json 导出：固定外层键 + result 原始结构，ensure_ascii 保中文。"""
    payload = {
        "task_id": task.id,
        "task_name": task.name,
        "evaluation_type": task.evaluation_type,
        "execution_number": record.execution_number,
        "status": record.status,
        "started_at": _iso(record.started_at),
        "completed_at": _iso(record.completed_at),
        "total_cost": record.total_cost,
        "error_message": record.error_message,
        "config_snapshot": record.config_snapshot,
        "result": record.result,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def export_filename(task_name: str, execution_number: int, fmt: str,
                    completed_at: Optional[datetime] = None) -> str:
    """导出文件名：{任务名}_exec{n}_{时间戳}.{md|json}（hub_file_service 清洗）。"""
    from app.services.hub_file_service import sanitize_hub_name

    ext = {"markdown": "md", "json": "json"}.get(fmt, fmt)
    ts = (completed_at or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    base = f"{task_name or 'task'}_exec{execution_number}_{ts}.{ext}"
    return sanitize_hub_name(base, fallback="aievo_export")


def build_markdown_export(record: Any, task: Any) -> bytes:
    """markdown 全量报告；failed/stopped 只出失败简报。"""
    lines: List[str] = []
    result = record.result if isinstance(record.result, dict) else (record.result or {})

    _emit_header(lines, record, task)

    if record.status in ("failed", "stopped") and not _detail_rows(result):
        _emit_failure_brief(lines, record, result)
    else:
        _emit_config_summary(lines, record)
        _emit_summary_metrics(lines, result)
        _emit_details(lines, result)

    return "\n".join(lines).encode("utf-8")


# ── 头部与失败简报 ──────────────────────────────────────────────────


def _emit_header(lines: List[str], record: Any, task: Any) -> None:
    status_label = {"completed": "已完成", "failed": "失败", "stopped": "已停止",
                    "running": "执行中", "pending": "等待中"}.get(record.status, record.status)
    lines.append(f"# 评测执行报告：{task.name}")
    lines.append("")
    lines.append(f"- 任务 ID：`{task.id}`")
    lines.append(f"- 评测类型：{task.evaluation_type}")
    lines.append(f"- 执行序号：第 {record.execution_number} 次")
    lines.append(f"- 状态：{status_label}")
    lines.append(f"- 开始时间：{_iso(record.started_at) or '（空）'}")
    lines.append(f"- 完成时间：{_iso(record.completed_at) or '（空）'}")
    if record.completed_at and record.started_at:
        delta = record.completed_at - record.started_at
        lines.append(f"- 耗时：{delta}")
    if record.total_cost is not None:
        lines.append(f"- 消耗成本：{record.total_cost}")
    lines.append("")


def _emit_failure_brief(lines: List[str], record: Any, result: Dict[str, Any]) -> None:
    lines.append("## 失败简报")
    lines.append("")
    lines.append(f"- 状态：{record.status}")
    error = record.error_message or (result or {}).get("error")
    if error:
        lines.append("")
        lines.append("**错误信息：**")
        lines.append("")
        lines.append("```text")
        lines.append(str(error))
        lines.append("```")
    lines.append("")


# ── 配置摘要与汇总指标 ──────────────────────────────────────────────


def _emit_config_summary(lines: List[str], record: Any) -> None:
    snapshot = record.config_snapshot
    if not isinstance(snapshot, dict) or not snapshot:
        return
    lines.append("## 配置摘要")
    lines.append("")
    _emit_kv_block(lines, snapshot, top_level=True)
    lines.append("")


def _emit_summary_metrics(lines: List[str], result: Dict[str, Any]) -> None:
    scalars = {
        k: v for k, v in result.items()
        if k not in _DETAIL_ARRAY_KEYS and k != "dimension_scores" and not isinstance(v, (list, dict))
    }
    dim_scores = result.get("dimension_scores")
    if not scalars and not isinstance(dim_scores, dict):
        return
    lines.append("## 汇总指标")
    lines.append("")
    for key in sorted(scalars):
        lines.append(f"- {key}：{_render_scalar(scalars[key])}")
    if isinstance(dim_scores, dict) and dim_scores:
        lines.append("")
        lines.append("| 维度 | 平均分 | 权重 | 样本数 |")
        lines.append("| --- | --- | --- | --- |")
        for dim, info in dim_scores.items():
            if isinstance(info, dict):
                lines.append(
                    f"| {dim} | {info.get('average', '（空）')} "
                    f"| {info.get('weight', '（空）')} | {info.get('count', '（空）')} |"
                )
            else:
                lines.append(f"| {dim} | {info} | （空） | （空） |")
    # 汇总区里其余非明细 dict/list（各类型自定义摘要，如 metrics_summary）逐块 json 呈现
    for key, value in result.items():
        if key in _DETAIL_ARRAY_KEYS or key == "dimension_scores":
            continue
        if isinstance(value, (list, dict)) and value:
            lines.append("")
            lines.append(f"**{key}**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            lines.append("```")
    lines.append("")


# ── 逐题明细 ────────────────────────────────────────────────────────


def _detail_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """定位明细数组并合并排序：items+failed_items（judge/plugin）→ detail_results（rag）→ records（latency）。"""
    items = result.get("items")
    failed = result.get("failed_items")
    if isinstance(items, list) and items:
        rows = [r for r in (list(items) + list(failed or [])) if isinstance(r, dict)]
    elif isinstance(result.get("detail_results"), list) and result["detail_results"]:
        rows = [r for r in result["detail_results"] if isinstance(r, dict)]
    elif isinstance(result.get("records"), list) and result["records"]:
        rows = [r for r in result["records"] if isinstance(r, dict)]
    else:
        rows = []
    return sorted(rows, key=lambda r: r.get("idx") if isinstance(r.get("idx"), int) else 0)


def _emit_details(lines: List[str], result: Dict[str, Any]) -> None:
    rows = _detail_rows(result)
    if not rows:
        return
    items = result.get("items")
    failed_count = len(result["failed_items"]) if isinstance(result.get("failed_items"), list) else 0
    total = len(items) + failed_count if isinstance(items, list) else len(rows)
    lines.append(f"## 每题明细（共 {total} 题）")
    lines.append("")
    for row in rows:
        _emit_one_row(lines, row)
    lines.append("")


def _emit_one_row(lines: List[str], row: Dict[str, Any]) -> None:
    idx = row.get("idx", "？")
    ok = row.get("success") is True
    mark = "✅" if ok else "❌"
    row_status = row.get("row_status")
    title = f"### 第 {idx} 题 {mark}"
    if row_status and row_status != "completed":
        title += f"（{row_status}）"
    lines.append(title)
    lines.append("")

    # 输入字段：排除结构键后的平铺键（question/user_input/ref_answer 等），全文不截断
    inputs = {k: v for k, v in row.items() if k not in _ITEM_STRUCTURAL_KEYS}
    if inputs:
        lines.append("**输入：**")
        lines.append("")
        _emit_kv_block(lines, inputs)
        lines.append("")

    if row.get("error"):
        lines.append("**错误：**")
        lines.append("")
        lines.append("```text")
        lines.append(str(row["error"]))
        lines.append("```")
        lines.append("")

    if "model_output" in row and row["model_output"] is not None:
        lines.append("**模型输出：**")
        lines.append("")
        lines.append("```text")
        lines.append(str(row["model_output"]))
        lines.append("```")
        lines.append("")

    if isinstance(row.get("average_score"), (int, float)):
        lines.append(f"**行得分：{row['average_score']}**")
        lines.append("")

    row_dims = row.get("dimension_scores")
    if isinstance(row_dims, dict) and row_dims:
        lines.append("**行级维度均分：** " + json.dumps(row_dims, ensure_ascii=False, default=str))
        lines.append("")

    judge_details = row.get("judge_details")
    if isinstance(judge_details, list) and judge_details:
        lines.append("**裁判评语：**")
        lines.append("")
        for jd in judge_details:
            if not isinstance(jd, dict):
                continue
            _emit_judge_detail(lines, jd)
        lines.append("")
    elif row.get("feedback"):
        lines.append("**评语：**")
        lines.append("")
        lines.append(str(row["feedback"]))
        lines.append("")

    if isinstance(row.get("pre_script_results"), (list, dict)) and row["pre_script_results"]:
        lines.append("**前置脚本结果：**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(row["pre_script_results"], ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")


def _emit_judge_detail(lines: List[str], jd: Dict[str, Any]) -> None:
    name = jd.get("evaluation_target_name") or f"裁判 {jd.get('judge_index', '？')}"
    score = jd.get("score", "（空）")
    head = f"- **{name}：{score} 分**"
    if jd.get("judge_failed"):
        head += f"（裁判失败：{jd.get('fail_reason') or '未知原因'}）"
    lines.append(head)
    for key in _JD_FEEDBACK_KEYS:
        value = jd.get(key)
        if not value:
            continue
        if key == "feedback" and isinstance(value, str) and value.strip():
            lines.append("")
            lines.append(f"  {value}")
        elif isinstance(value, dict) and value:
            lines.append(f"  - {key}：{json.dumps(value, ensure_ascii=False, default=str)}")


# ── 通用键值渲染 ────────────────────────────────────────────────────


def _render_scalar(value: Any) -> str:
    if value is None:
        return "（空）"
    if isinstance(value, str):
        return value if value else "（空字符串）"
    return json.dumps(value, ensure_ascii=False, default=str)


def _emit_kv_block(lines: List[str], data: Dict[str, Any], top_level: bool = False) -> None:
    """键值块：标量走列表项，长文本/嵌套结构走独立段落或 json 代码块。"""
    for key, value in data.items():
        if isinstance(value, (dict, list)) and value:
            lines.append(f"- **{key}**：")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            lines.append("```")
        elif isinstance(value, str) and ("\n" in value or len(value) > 120):
            lines.append(f"- **{key}**：")
            lines.append("")
            lines.append("```text")
            lines.append(value)
            lines.append("```")
        else:
            lines.append(f"- {key}：{_render_scalar(value)}")
