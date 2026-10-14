"""judge（裁判）评测类型适配器（W17 逐类型迁移之一）

执行：judge/agent 是执行分发的默认通道（未注册执行钩子 → 调用方回落
``EvaluationExecutor`` 裁判批量执行），故不提供 evaluate_batch。
聚合：任务级聚合走 ``ResultAggregator(task).aggregate``（与迁移前
``aggregate_for_task`` 的默认回落完全同口径）。
导出：excel/csv 的 judge 分支体自 ``result_aggregator.py`` 迁入，
输出逐字节等价（tests/golden 门禁）。
"""
import io
import json
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


@evaluator_registry.register
class JudgeEvaluator(BaseEvaluator):
    """judge / agent 默认评测类型适配器"""

    evaluation_type = "judge"

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from app.services.result_aggregator import ResultAggregator

        return ResultAggregator(task).aggregate(results)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 裁判评测结果格式 — 动态输入变量列 + 固定指标列
        input_cols = ResultExporter._collect_input_columns(results)
        data = []
        for r in results:
            row = {"题目序号": r.get("question_index", "")}
            # 动态输入变量
            for col in input_cols:
                row[col] = str(r.get(col, ""))[:200]
            # 固定指标
            row["模型回答"] = str(r.get("model_answer", ""))[:100]
            row["综合评分"] = r.get("overall_score", "")
            row["评分详情"] = json.dumps(r.get("scores", {}), ensure_ascii=False)
            row["评分理由"] = r.get("reasoning", "")
            data.append(row)
        df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="评测结果")

        return output.getvalue()

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        import pandas as pd

        from app.services.result_aggregator import ResultExporter

        # 裁判评测 — 动态输入变量列 + 固定指标列
        input_cols = ResultExporter._collect_input_columns(results)
        data = []
        for r in results:
            row = {"question_index": r.get("question_index", "")}
            for col in input_cols:
                row[col] = str(r.get(col, ""))[:200]
            row["model_answer"] = str(r.get("model_answer", ""))[:200]
            row["overall_score"] = r.get("overall_score", "")
            data.append(row)
        df = pd.DataFrame(data)

        return df.to_csv(index=False)
