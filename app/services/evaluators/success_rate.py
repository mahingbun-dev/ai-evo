"""success_rate（成功率）评测类型适配器（W17 逐类型迁移之三）

执行：委托 ``evaluation_tasks_async._execute_success_rate_evaluation``
（位置实参与迁移前分支一致：task, payloads, task_id, db, stop_event）。
聚合：委托 ``ResultAggregator.aggregate_success_rate_results``。
导出：excel 的 success_rate 分支体自 ``result_aggregator.py`` 迁入；
csv 无专属分支（迁移前即走通用回落），不覆写 csv 钩子。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class SuccessRateEvaluatorAdapter(BaseEvaluator):
    """success_rate 评测类型适配器"""

    evaluation_type = "success_rate"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.tasks import evaluation_tasks_async

        return evaluation_tasks_async._execute_success_rate_evaluation(
            task, payloads, ctx.task_id, ctx.db, ctx.stop_event,
            execution_number=ctx.execution_number,
        )

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        return ResultAggregator.aggregate_success_rate_results(results)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 成功率评测结果格式 — 动态输入变量列 + 固定指标列
        input_cols = ResultExporter._collect_input_columns(results)
        data = []
        for r in results:
            metrics = r.get("metrics", {}) or {}
            row = {"题目序号": r.get("idx", 0) + 1}
            for col in input_cols:
                row[col] = str(r.get(col, ""))[:200]
            row["总调用次数"] = metrics.get("total_calls", "")
            row["成功次数"] = metrics.get("successful_calls", "")
            row["成功率"] = metrics.get("success_rate", "")
            row["平均响应时间"] = metrics.get("avg_response_time", "")
            data.append(row)
        df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()
