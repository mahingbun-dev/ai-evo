"""plugin（插件评测）评测类型适配器（W17 架构接入）

执行：委托 ``evaluation_tasks_async._execute_plugin_evaluation``
（quick 静态层 / standard 静态 + judge 层，位置实参与既有专用路径一致）。
聚合：委托 ``ResultAggregator.aggregate_plugin_results``。
导出：excel/csv 均为 plugin 专属列（逐插件明细：复合分/徽章/置信标签/反模式/维度分）。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


def _market_commit_label(item: Dict[str, Any]) -> str:
    """导出用的版本锚点标签：市场来源取 commit（同步降级时标注 stale）"""
    market = item.get("market") or {}
    commit = str(market.get("commit") or "")
    if not commit:
        return ""
    return f"{commit}（本地副本）" if market.get("stale") else commit


def _evaluator_label(item: Dict[str, Any]) -> str:
    """导出用的评测器（评测动作）版本标签：plugin-eval 版本@commit"""
    evaluator = item.get("evaluator") or {}
    if not evaluator:
        return ""
    version = str(evaluator.get("version") or "?")
    commit = str(evaluator.get("commit") or "?")
    label = f"plugin-eval {version}@{commit}"
    return f"{label}（本地副本）" if evaluator.get("stale") else label


def _direction_cells(item: Dict[str, Any]) -> Dict[str, Any]:
    """方向分项（报告「分项」表）逐方向一列：可信度（判定有效率，可判定/适用）

    列固定为 plugin-eval 的方向全集（缺失留空），保证导出表列序稳定；
    未测阶段没有检查项，不参与（否则会导出一整行 0/0 的假分项）。
    """
    from app.services.plugin_evaluator import HISTORY_DIMENSION_LABELS

    cells: Dict[str, Any] = {label: "" for label in HISTORY_DIMENSION_LABELS.values()}
    for phase in (item.get("trace") or {}).get("phases") or []:
        if phase.get("status") == "unmeasured" or phase.get("detected") is False:
            continue
        for dim in phase.get("dimensions") or []:
            label = str(dim.get("label") or dim.get("key") or "")
            if not label or label not in cells or cells[label]:
                continue
            score, coverage = dim.get("score"), dim.get("coverage")
            cells[label] = (
                f"{'未测' if score is None else round(float(score), 2)}"
                f"（有效率 {'未测' if coverage is None else str(round(float(coverage), 2)) + '%'}"
                f"，{dim.get('measured', 0)}/{dim.get('applicable', 0)}）"
            )
    return cells


def _phase_cells(item: Dict[str, Any]) -> Dict[str, Any]:
    """逐阶段得分/判定有效率（报告「测评总览」的生成/执行两行）"""
    cells: Dict[str, Any] = {"阶段得分": "", "未测阶段": ""}
    unmeasured = []
    parts = []
    for phase in (item.get("trace") or {}).get("phases") or []:
        label = str(phase.get("label") or phase.get("metric") or "")
        if phase.get("status") == "unmeasured" or phase.get("detected") is False:
            unmeasured.append(label)
            continue
        score, coverage = phase.get("score"), phase.get("coverage")
        counts = phase.get("counts") or {}
        parts.append(
            f"{label} {'未测' if score is None else round(float(score), 2)}"
            f"（有效率 {'未测' if coverage is None else str(round(float(coverage), 2)) + '%'}"
            f"，{counts.get('measured', 0)}/{counts.get('applicable', 0)}）"
        )
    if parts:
        cells["阶段得分"] = "；".join(parts)
    if unmeasured:
        cells["未测阶段"] = "、".join(unmeasured)
    return cells


def _real_data_cells(item: Dict[str, Any]) -> Dict[str, Any]:
    """trace 口径专属列（非 trace 行全为空串，导出表结构统一）"""
    real_data = item.get("real_data") or {}
    trace = item.get("trace") or {}
    if not real_data and not trace:
        return {
            "得分口径": "",
            "得分标签": "",
            "综合分": "",
            "真实数据分": "",
            "可判定/适用": "",
            "判定有效率": "",
            "行为覆盖率": "",
            "通过": "",
            "失败": "",
            "证据不足": "",
            "失败项": "",
            "识别插件": "",
            "线程文件": "",
            "线程SHA256": "",
            "历史规则": "",
            "未测原因": "",
        }
    counts = real_data.get("counts") or {}
    rule = trace.get("rule") or {}
    detected = trace.get("detected") or {}
    behavior = real_data.get("behavior_coverage") or {}
    plugin_name = str(detected.get("plugin") or "")
    plugin_version = str(trace.get("plugin_version") or "")
    failed = [
        check
        for phase in trace.get("phases") or []
        for check in phase.get("failed_checks") or []
    ]
    counts_all = (real_data.get("counts") or {})
    decidable = (counts_all.get("passed") or 0) + (counts_all.get("failed") or 0)
    applicable = decidable + (counts_all.get("insufficient") or 0)
    # 旧落库的 trace 行（改动前只有真实数据分）没有 composite_score 键：
    # 其「复合分」列装的是真实数据分，必须清掉，否则同一行的两列会重复且名不副实
    legacy_trace = "composite_score" not in item
    score_kind = "历史口径（仅真实数据分）" if legacy_trace else "合并口径（综合分 + 真实数据分）"
    cells = {
        # 得分口径：合并口径下同一行有两套独立分数，导出必须标明哪一列是哪一套
        "得分口径": score_kind,
        # 得分标签与样本量同行导出：得分分母只有「通过 + 失败」，证据不足不计分也不等于通过
        "得分标签": (
            "部分样本得分"
            if str(real_data.get("status")) != "complete"
            or real_data.get("extraction_complete") is False
            else "真实数据得分"
        ),
        "综合分": item.get("composite_score", ""),
        "真实数据分": real_data.get("score", ""),
        "可判定/适用": f"{decidable} / {applicable}",
        "判定有效率": real_data.get("coverage", ""),
        # 覆盖率列名随口径切换：key_steps=关键步骤覆盖率，all=行为覆盖率
        (
            "关键步骤覆盖率" if behavior.get("scope") == "key_steps" else "行为覆盖率"
        ): behavior.get("rate", ""),
        "通过": counts.get("passed", ""),
        "失败": counts.get("failed", ""),
        "证据不足": counts.get("insufficient", ""),
        "设计质量复用": "是" if item.get("design_reused") else ("否" if "design_reused" in item else ""),
        # 失败项逐条留档（结论要能离线复核，故不进 JSONB 明细上限）
        "失败项": "；".join(
            f"{check.get('dimension_label') or check.get('dimension')} "
            f"{check.get('rule_id')}@{check.get('subject')}：{check.get('reason') or ''}"
            for check in failed
        ),
        "识别插件": f"{plugin_name} v{plugin_version}".strip() if plugin_version else plugin_name,
        "线程文件": trace.get("thread_file", ""),
        "线程SHA256": trace.get("thread_sha256", ""),
        "历史规则": (
            f"{rule.get('file')} v{rule.get('version')}".strip() if rule.get("file") else ""
        ),
        "未测原因": trace.get("unavailable_reason", ""),
    }
    if legacy_trace:
        cells["复合分"] = ""
    return cells


@evaluator_registry.register
class PluginEvaluatorAdapter(BaseEvaluator):
    """plugin 评测类型适配器"""

    evaluation_type = "plugin"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.tasks import evaluation_tasks_async

        return evaluation_tasks_async._execute_plugin_evaluation(
            task, payloads, ctx.task_id, ctx.db, ctx.stop_event,
            execution_number=ctx.execution_number,
        )

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        return ResultAggregator.aggregate_plugin_results(results)

    def _collect_items(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """聚合结构（items）展开；兼容直流行结果无 items 键的场景"""
        items: List[Dict[str, Any]] = []
        for r in results:
            if "items" in r:
                items.extend(r.get("items", []))
        return items

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        items = self._collect_items(results)
        data = []
        for idx, item in enumerate(items):
            anti_flags = [ap.get("flag", "") for ap in item.get("anti_patterns", [])]
            row = {
                "序号": (item.get("idx", idx) if item.get("idx") is not None else idx) + 1,
                "评测对象": item.get("target_name", ""),
                "对象类型": item.get("target_type", ""),
                "插件版本": _market_commit_label(item),
                "评测器版本": _evaluator_label(item),
                "复合分": item.get("score", ""),
                "徽章": item.get("badge", ""),
                "置信标签": item.get("confidence_label", ""),
                "评测深度": item.get("depth", ""),
                "反模式惩罚": item.get("anti_pattern_penalty", ""),
                "反模式": ", ".join(f for f in anti_flags if f),
                "警告": "; ".join(item.get("warnings", [])),
            }
            for dim_name, dim_value in (item.get("dimension_scores") or {}).items():
                row[f"维度:{dim_name}"] = dim_value
            row.update(_real_data_cells(item))
            row.update(_direction_cells(item))
            row.update(_phase_cells(item))
            data.append(row)
        df = pd.DataFrame(data)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        import pandas as pd

        items = self._collect_items(results)
        data = []
        for idx, item in enumerate(items):
            anti_flags = [ap.get("flag", "") for ap in item.get("anti_patterns", [])]
            row = {
                "index": (item.get("idx", idx) if item.get("idx") is not None else idx) + 1,
                "target_name": item.get("target_name", ""),
                "target_type": item.get("target_type", ""),
                "market_commit": _market_commit_label(item),
                "evaluator": _evaluator_label(item),
                "score": item.get("score", ""),
                "badge": item.get("badge", ""),
                "confidence_label": item.get("confidence_label", ""),
                "depth": item.get("depth", ""),
                "anti_patterns": ", ".join(f for f in anti_flags if f),
                "warnings": "; ".join(item.get("warnings", [])),
            }
            for dim_name, dim_value in (item.get("dimension_scores") or {}).items():
                row[f"dim:{dim_name}"] = dim_value
            row.update(_real_data_cells(item))
            row.update(_direction_cells(item))
            row.update(_phase_cells(item))
            data.append(row)
        df = pd.DataFrame(data)

        return df.to_csv(index=False)
