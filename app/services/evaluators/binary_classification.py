"""binary_classification（二分分类）评测类型适配器（W17 逐类型迁移之五）

执行：委托 ``evaluation_tasks_async._execute_binary_classification_evaluation``
（位置实参与迁移前分支一致：task, payloads, task_id, db, stop_event）。
聚合：委托 ``ResultAggregator.aggregate_binary_classification_results``
（binary_metrics_config 取 task 口径字段，缺省 []）。
导出：excel（统计摘要+逐条明细双 Sheet）与 csv 分支体自
``result_aggregator.py`` 迁入。
"""
import io
import json
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class BinaryClassificationEvaluatorAdapter(BaseEvaluator):
    """binary_classification 评测类型适配器"""

    evaluation_type = "binary_classification"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.tasks import evaluation_tasks_async

        return evaluation_tasks_async._execute_binary_classification_evaluation(
            task, payloads, ctx.task_id, ctx.db, ctx.stop_event,
            execution_number=ctx.execution_number,
        )

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        binary_metrics_config = getattr(task, "binary_metrics_config", None) or []
        return ResultAggregator.aggregate_binary_classification_results(
            results, binary_metrics_config
        )

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 二分分类评测结果格式 — 统计摘要 + 逐条明细（两个 Sheet）
        result_data = None
        for r in results:
            if "items" in r:
                result_data = r
                break

        items = result_data.get("items", []) if result_data else []
        input_cols = ResultExporter._collect_input_columns(items)

        # Sheet 1: 统计摘要
        summary_data = ResultExporter._build_binary_classification_summary(result_data)
        df_summary = pd.DataFrame(summary_data)

        # Sheet 2: 逐条明细
        detail_data = []
        for idx, item in enumerate(items):
            row = {"序号": (item.get("idx", idx) if item.get("idx") is not None else idx) + 1}
            for col in input_cols:
                row[col] = str(item.get(col, ""))[:200]
            row["期望标签"] = item.get("expected_label", "")
            row["预测标签"] = item.get("predicted_label", "")
            row["判定结果"] = "正确" if item.get("is_correct") else "错误"
            row["耗时(秒)"] = round(item.get("duration", 0), 3) if item.get("duration") else ""
            detail_data.append(row)
        df_detail = pd.DataFrame(detail_data)

        # 导出到两个 Sheet
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df_summary.to_excel(writer, index=False, sheet_name="统计摘要")
            df_detail.to_excel(writer, index=False, sheet_name="逐条明细")

        return output.getvalue()

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 二分分类评测结果格式 — 动态输入变量列
        data = []
        for r in results:
            items = r.get("items", [])
            input_cols = ResultExporter._collect_input_columns(items)
            for idx, item in enumerate(items):
                row = {"index": (item.get("idx", idx) if item.get("idx") is not None else idx) + 1}
                for col in input_cols:
                    row[col] = str(item.get(col, ""))[:200]
                row["expected_label"] = item.get("expected_label", "")
                row["predicted_label"] = item.get("predicted_label", "")
                row["is_correct"] = item.get("is_correct", "")
                row["duration"] = round(item.get("duration", 0), 3) if item.get("duration") else ""
                data.append(row)
            if not data:
                cm = r.get("confusion_matrix", {})
                data.append({
                    "index": "",
                    "expected_label": "",
                    "predicted_label": "",
                    "is_correct": f"TP:{cm.get('tp',0)} FP:{cm.get('fp',0)} TN:{cm.get('tn',0)} FN:{cm.get('fn',0)}",
                    "duration": "",
                })
        df = pd.DataFrame(data)

        return df.to_csv(index=False)
