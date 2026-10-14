"""consistency（一致性）评测类型适配器（W17 逐类型迁移之二）

执行：委托 ``evaluation_tasks_async._execute_consistency_evaluation``
（函数体内延迟导入，保持既有直调/打桩契约与调用点稳定）。
聚合：委托 ``ResultAggregator.aggregate_consistency_results``（与迁移前
``aggregate_for_task`` 的 consistency 分支同口径）。
导出：excel 的 consistency 分支体自 ``result_aggregator.py`` 迁入；
csv 无 consistency 专属分支（迁移前即走通用回落），不覆写 csv 钩子。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class ConsistencyEvaluatorAdapter(BaseEvaluator):
    """consistency 评测类型适配器"""

    evaluation_type = "consistency"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        # 延迟导入：适配器与执行模块互相不构成导入环；经模块属性取函数，
        # 既有 patch("app.tasks.evaluation_tasks_async._execute_consistency_evaluation")
        # 打桩方式继续生效
        from app.tasks import evaluation_tasks_async

        return evaluation_tasks_async._execute_consistency_evaluation(
            task, payloads, ctx.db, ctx.task_id, ctx.stop_event,
            execution_number=ctx.execution_number,
        )

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        return ResultAggregator.aggregate_consistency_results(results)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 一致性评测结果格式 — 动态输入变量列 + 固定指标列
        input_cols = ResultExporter._collect_input_columns(results)
        data = []
        for r in results:
            metrics = r.get("metrics", {}) or {}
            row = {"题目序号": r.get("idx", 0) + 1}
            # 动态输入变量
            for col in input_cols:
                row[col] = str(r.get(col, ""))[:200]
            # 固定指标
            row["重复次数"] = metrics.get("total_calls", "")
            row["成功次数"] = metrics.get("successful_calls", "")
            row["文本相似度"] = metrics.get("text_similarity", "")
            row["格式一致性"] = metrics.get("format_consistency", "")
            row["结构一致性"] = metrics.get("structure_consistency", "")
            data.append(row)
        df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()
