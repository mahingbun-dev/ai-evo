"""rag（检索质量）评测类型适配器（W17 逐类型迁移之四）

执行：委托 ``evaluation_tasks_async._execute_rag_evaluation``
（位置实参与迁移前分支一致：task, payloads, task_id, stop_event,
langfuse_host，另带执行序号与续跑偏移关键字实参）。
聚合：委托 ``ResultAggregator.aggregate_rag_results``。
导出：excel 的 rag 分支体自 ``result_aggregator.py`` 迁入；
csv 无专属分支（迁移前即走通用回落），不覆写 csv 钩子。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class RAGEvaluatorAdapter(BaseEvaluator):
    """rag 评测类型适配器"""

    evaluation_type = "rag"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.tasks import evaluation_tasks_async

        return evaluation_tasks_async._execute_rag_evaluation(
            task, payloads, ctx.task_id, ctx.stop_event, ctx.langfuse_host,
            execution_number=ctx.execution_number,
            progress_offset=ctx.progress_offset,
        )

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        return ResultAggregator.aggregate_rag_results(results)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # RAG 检索质量评测结果格式 — 动态输入变量列 + 固定指标列
        input_cols = ResultExporter._collect_input_columns(results)
        data = []
        for idx, r in enumerate(results):
            scores = r.get("scores", {})
            row = {"题目序号": r.get("idx", idx) + 1}
            # 动态输入变量
            for col in input_cols:
                row[col] = str(r.get(col, ""))[:200]
            # 固定指标
            row["上下文精度(Context Precision)"] = scores.get("context_precision", "")
            row["上下文召回(Context Recall)"] = scores.get("context_recall", "")
            row["命中率(Hit Rate)"] = scores.get("hit_rate", "")
            row["平均倒数排名(MRR)"] = scores.get("mrr", "")
            row["抗噪声能力(Noise Sensitivity)"] = scores.get("noise_sensitivity", "")
            row["拒绝能力(Negative Rejection)"] = scores.get("negative_rejection", "")
            row["执行状态"] = "成功" if r.get("success") else f"失败: {r.get('error', '')}"
            data.append(row)
        df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()
