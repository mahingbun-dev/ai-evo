"""safety（安全评测）类型适配器（M3 QE3-1）

执行：委托 ``EvaluationExecutor.execute_batch_safety``——变异增强 → 红队
攻击被测模型 → 双通道判定（确定性检测器 + 裁判 rubric），行级 write-through
落库与既有链路共用。
聚合：红队统计口径——总体防御率 + 按攻击面类目分组（样本数/违规数/防御率）
+ 双通道命中分布 + 变异体单独统计（变异体是对「换皮攻击」的泛化检验，
不与原始样本混算）+ 证据行明细。
导出：excel/csv 安全专视图（类目/样本/攻击内容/模型回复/双通道判定/证据）。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.registry import evaluator_registry


def aggregate_safety_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """安全评测任务级聚合（执行 complete 与停止部分结果共用同一口径）

    行级 ``safety`` 判定结构见 ``RedTeamSafetyEngine.evaluate_reply``；
    行执行失败（success=False 无 safety 块）计入 errors 不计入防御率分母。
    """
    from app.services.redteam.engine import RedTeamSafetyEngine

    executed_rows = [r for r in results if isinstance(r, dict) and r.get("safety")]
    error_rows = [r for r in results if isinstance(r, dict) and not r.get("safety")]

    def _row_category(row: Dict[str, Any]) -> str:
        return row.get("attack_category") or "uncategorized"

    # 类目分组统计
    by_category: Dict[str, Dict[str, Any]] = {}
    for row in executed_rows:
        cat = _row_category(row)
        bucket = by_category.setdefault(cat, {
            "name": RedTeamSafetyEngine.surface_label(cat),
            "total": 0, "violations": 0, "defense_rate": 0.0,
        })
        bucket["total"] += 1
        if row["safety"].get("violation"):
            bucket["violations"] += 1

    # 双通道命中分布（+ deepeval metric 开关，M3 QE3-2）
    channel_stats = {
        "deterministic": {"triggered": 0},
        "judge_rubric": {"violations": 0, "unavailable": 0},
        "deepeval": {"violations": 0, "unavailable": 0, "metrics": {}},
    }
    for row in executed_rows:
        channels = (row["safety"] or {}).get("channels") or {}
        det = channels.get("deterministic") or {}
        judge = channels.get("judge_rubric") or {}
        deepeval = channels.get("deepeval") or {}
        if det.get("triggered"):
            channel_stats["deterministic"]["triggered"] += 1
        if judge.get("violation"):
            channel_stats["judge_rubric"]["violations"] += 1
        if judge.get("unavailable"):
            channel_stats["judge_rubric"]["unavailable"] += 1
        if deepeval:
            row_failed = False
            row_unavailable = False
            for metric_key, metric in deepeval.items():
                if not isinstance(metric, dict):
                    continue
                bucket = channel_stats["deepeval"]["metrics"].setdefault(
                    metric_key, {"violations": 0, "unavailable": 0}
                )
                if metric.get("success") is False and not metric.get("metric_unavailable"):
                    bucket["violations"] += 1
                    row_failed = True
                if metric.get("metric_unavailable"):
                    bucket["unavailable"] += 1
                    row_unavailable = True
            if row_failed:
                channel_stats["deepeval"]["violations"] += 1
            if row_unavailable:
                channel_stats["deepeval"]["unavailable"] += 1

    # 变异体单独统计（不与原始样本混算）
    mutation_rows = [r for r in executed_rows if r.get("is_mutation")]
    mutation_violations = sum(1 for r in mutation_rows if r["safety"].get("violation"))

    total = len(executed_rows)
    violations = sum(1 for r in executed_rows if r["safety"].get("violation"))
    for bucket in by_category.values():
        bucket["defense_rate"] = round(
            (bucket["total"] - bucket["violations"]) / bucket["total"], 4
        ) if bucket["total"] else 0.0

    return {
        "total": len(results),
        "executed": total,
        "violations": violations,
        "defense_rate": round((total - violations) / total, 4) if total else 0.0,
        "by_category": by_category,
        "by_channel": channel_stats,
        "mutation": {
            "variants": len(mutation_rows),
            "violations": mutation_violations,
            "defense_rate": round(
                (len(mutation_rows) - mutation_violations) / len(mutation_rows), 4
            ) if mutation_rows else None,
        },
        "errors": len(error_rows),
        "success_count": total - violations,
        "failed_count": violations,
        "items": results,
    }


@evaluator_registry.register
class SafetyEvaluatorAdapter(BaseEvaluator):
    """safety 评测类型适配器"""

    evaluation_type = "safety"

    def evaluate_batch(self, ctx, task, payloads: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(
            # safety 前端清空裁判数量（judge_count=0）：并发缺省 4（与 judge 链路上限一致）
            max_workers=min(task.judge_count or 4, 4),
            task_id=ctx.task_id,
            stop_event=ctx.stop_event,
            langfuse_host=ctx.langfuse_host,
            execution_number=ctx.execution_number,
            progress_offset=ctx.progress_offset,
        )
        return executor.execute_batch_safety(payloads, task)

    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return aggregate_safety_results(results)

    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        import io

        import pandas as pd

        from app.services.redteam.engine import RedTeamSafetyEngine

        data = []
        for r in results:
            safety = r.get("safety") or {}
            channels = safety.get("channels") or {}
            det = channels.get("deterministic") or {}
            det_findings = "; ".join(
                f"{f.get('label')}: {f.get('evidence', '')}" for f in (det.get("findings") or [])
            )
            judge = channels.get("judge_rubric") or {}
            data.append({
                "序号": r.get("idx", ""),
                "攻击面": RedTeamSafetyEngine.surface_label(r.get("attack_category", "")),
                "样本ID": r.get("attack_sample_id", ""),
                "变异体": "是" if r.get("is_mutation") else "否",
                "攻击内容": r.get("user_input", ""),
                "模型回复": r.get("model_output", ""),
                "综合判定": "违规" if safety.get("violation") else "防守成功",
                "严重度": safety.get("severity", ""),
                "确定性通道": "命中" if det.get("triggered") else "未命中",
                "确定性证据": det_findings,
                "裁判判定": ("违规" if judge.get("violation") else ("未违规" if judge.get("violation") is False else "不可用")),
                "裁判理由": judge.get("reason", ""),
                "裁判证据": judge.get("evidence", ""),
                "风险点": r.get("risk_point", ""),
                "执行错误": r.get("error", ""),
            })
        df = pd.DataFrame(data if data else [{
            "序号": "", "攻击面": "", "样本ID": "", "变异体": "", "攻击内容": "",
            "模型回复": "", "综合判定": "", "严重度": "", "确定性通道": "",
            "确定性证据": "", "裁判判定": "", "裁判理由": "", "裁判证据": "",
            "风险点": "", "执行错误": "",
        }])
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="安全评测结果")
        return output.getvalue()

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        import pandas as pd

        from app.services.redteam.engine import RedTeamSafetyEngine

        data = []
        for r in results:
            safety = r.get("safety") or {}
            channels = safety.get("channels") or {}
            det = channels.get("deterministic") or {}
            judge = channels.get("judge_rubric") or {}
            data.append({
                "index": r.get("idx", ""),
                "attack_surface": RedTeamSafetyEngine.surface_label(r.get("attack_category", "")),
                "sample_id": r.get("attack_sample_id", ""),
                "is_mutation": bool(r.get("is_mutation")),
                "attack_prompt": r.get("user_input", ""),
                "model_reply": r.get("model_output", ""),
                "verdict": "violation" if safety.get("violation") else "defended",
                "severity": safety.get("severity", ""),
                "deterministic_triggered": bool(det.get("triggered")),
                "judge_violation": judge.get("violation"),
                "judge_reason": judge.get("reason", ""),
                "error": r.get("error", ""),
            })
        df = pd.DataFrame(data)
        return df.to_csv(index=False)
