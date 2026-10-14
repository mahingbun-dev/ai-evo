"""latency（耗时）评测类型适配器（W17 逐类型迁移之六——最后一个分发分支）

执行：与迁移前分支逐行等价——专用 ``EvaluationExecutor`` 构造
（max_workers=min(judge_count,4)），``execute_batch_latency`` 批量执行；
耗时评测不做续跑过滤（展开行序号空间与数据行不一致），progress_offset 恒 0。
聚合：委托 ``ResultAggregator.calculate_latency_metrics``
（threshold 取 task.latency_config，缺省 10.0）。
导出：excel/csv 的 latency 分支体自 ``result_aggregator.py`` 迁入。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class LatencyEvaluatorAdapter(BaseEvaluator):
    """latency 评测类型适配器"""

    evaluation_type = "latency"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(
            max_workers=min(task.judge_count or 4, 4),
            task_id=ctx.task_id,
            stop_event=ctx.stop_event,
            langfuse_host=ctx.langfuse_host,
            execution_number=ctx.execution_number,
            # 耗时评测不做续跑过滤（展开行序号空间与数据行不一致），偏移恒 0
            progress_offset=0,
        )
        return executor.execute_batch_latency(payloads, task)

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        latency_config = getattr(task, "latency_config", None) or {}
        threshold = latency_config.get("threshold", 10.0)
        return ResultAggregator.calculate_latency_metrics(results, threshold=threshold)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        # 耗时评测结果格式
        data = []
        for r in results:
            records = r.get("records", []) or []
            for rec in records:
                data.append({
                    "序号": rec.get("index", ""),
                    "响应时间(秒)": round(rec.get("duration", 0), 3),
                    "状态": "通过" if rec.get("passed") else "失败",
                    "输入Token": rec.get("input_tokens") or "",
                    "输出Token": rec.get("output_tokens") or "",
                    "时间戳": rec.get("timestamp", ""),
                })
            # 如果没有单条记录（只有聚合结果），直接添加聚合数据
            if not data:
                data.append({
                    "序号": "",
                    "响应时间(秒)": "",
                    "状态": "",
                    "输入Token": "",
                    "输出Token": "",
                    "时间戳": "",
                })
        df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        import pandas as pd

        # 耗时评测结果格式
        data = []
        for r in results:
            records = r.get("records", []) or []
            for rec in records:
                data.append({
                    "index": rec.get("index", ""),
                    "duration_seconds": round(rec.get("duration", 0), 3),
                    "status": "passed" if rec.get("passed") else "failed",
                    "input_tokens": rec.get("input_tokens") or "",
                    "output_tokens": rec.get("output_tokens") or "",
                    "timestamp": rec.get("timestamp", ""),
                })
            if not data:
                data.append({
                    "index": "",
                    "duration_seconds": "",
                    "status": "",
                    "input_tokens": "",
                    "output_tokens": "",
                    "timestamp": "",
                })
        df = pd.DataFrame(data)

        return df.to_csv(index=False)
