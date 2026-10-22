"""
结果聚合与导出服务
"""
import json
import io
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.services.judge_evaluation_engine import JudgeEvaluationResult
from app.services.evaluators import evaluator_registry
from app.services.pass_k import aggregate_pass_k_rows


def _row_index(result: Dict[str, Any], fallback: int) -> int:
    """行序号取值口径：_orig_idx（行级落库事实源序号，persist_row_result/
    load_execution_results 注入）优先，其次 result 自带 idx，最后回退枚举序号。

    背景：judge/agent 默认执行路径曾有 payload 不带 idx 的回归（全部行
    idx=0 入库），行级表重建行的 row_result JSON 同样可能带着历史脏 idx，
    _orig_idx 是唯一可信的行身份。
    """
    orig = result.get("_orig_idx") if isinstance(result, dict) else None
    if isinstance(orig, int):
        return orig
    idx = result.get("idx", fallback) if isinstance(result, dict) else fallback
    return idx if isinstance(idx, int) else fallback


def _aggregate_status_rate(results: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """行级 {status: pass|fail|error} 结构的任务级通过率聚合（M2 共用口径）

    QE2-1（verification_result）与 QE2-2（trajectory_match）同构：分母为
    携带该键的行（模型调用失败等未到该步骤的行不计入——已按 failed 计入
    success_rate）；任务无任何该键行返回 None（聚合不写对应键，门禁引用
    相应 metric 按「不可用 → error」处理）。
    """
    statuses = [
        row.get(key)
        for row in results
        if isinstance(row, dict) and isinstance(row.get(key), dict)
    ]
    if not statuses:
        return None
    passed = sum(1 for s in statuses if s.get("status") == "pass")
    failed = sum(1 for s in statuses if s.get("status") == "fail")
    errored = sum(1 for s in statuses if s.get("status") == "error")
    return {
        "total": len(statuses),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "pass_rate": round(passed / len(statuses), 4),
    }


class ResultAggregator:
    """结果聚合服务"""

    def __init__(self, task=None):
        """
        初始化结果聚合器

        Args:
            task: 任务对象（可选）
        """
        self.task = task
        if task:
            self.max_score = task.max_score or 10
            self.evaluation_metrics = task.evaluation_metrics or []
        else:
            self.max_score = 10
            self.evaluation_metrics = []

    def calculate_overall_metrics(
        self,
        results: List[JudgeEvaluationResult],
        threshold: float = 0.6
    ) -> Dict[str, Any]:
        """
        计算整体评测指标（仅 judge 类型）

        Args:
            results: JudgeEvaluationResult 结果列表
            threshold: 评测通过阈值（默认0.6，即60分）

        Returns:
            包含以下指标的字典:
            - accuracy: 准确率 = (TP + TN) / Total
            - precision: 精确率 = TP / (TP + FP)
            - recall: 召回率 = TP / (TP + FN)
            - specificity: 特异度 = TN / (TN + FP)
            - avg_duration: 平均评测耗时（秒）
            - total_count: 总评测数
            - passed_count: 通过数
            - failed_count: 失败数
            - tp, fp, tn, fn: 混淆矩阵四个元素
        """
        if not results:
            return {
                "accuracy": 0.0,
                "precision": 0.0,
                "recall": 0.0,
                "specificity": 0.0,
                "avg_duration": 0.0,
                "total_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "tp": 0,
                "fp": 0,
                "tn": 0,
                "fn": 0,
            }

        tp = fp = tn = fn = 0
        total_duration = 0.0
        passed_count = 0

        for r in results:
            # 预测为正：评测通过（接口成功且分数达标）
            predicted_positive = r.success and (r.overall_score >= threshold)
            # 实际为正：API调用成功（独立于预测）
            actual_positive = r.success

            if predicted_positive and actual_positive:
                tp += 1
            elif predicted_positive and not actual_positive:
                fp += 1
            elif not predicted_positive and actual_positive:
                fn += 1
            else:
                tn += 1

            if predicted_positive:
                passed_count += 1

            total_duration += r.duration

        total_count = len(results)
        failed_count = total_count - passed_count

        # 避免除零
        accuracy = (tp + tn) / total_count if total_count > 0 else 0.0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        specificity = tn / (tn + fp) if (tn + fp) > 0 else 0.0
        avg_duration = total_duration / total_count if total_count > 0 else 0.0

        return {
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "specificity": round(specificity, 4),
            "avg_duration": round(avg_duration, 4),
            "total_count": total_count,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
        }

    @staticmethod
    def aggregate_for_task(task, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按任务评测类型分发聚合（W3：complete 聚合与 stop 部分结果共用同一口径）

        与 evaluation_tasks_async._aggregate_results 原分发逻辑逐分支一致，
        收敛到本单点维护；断点续跑/停止场景从行级表重建的行结果走完全
        相同的聚合，保证 result JSON 与旧行为同构。
        """
        evaluation_type = getattr(task, "evaluation_type", None)

        # W17：类型聚合收敛注册表——已迁移类型由适配器提供聚合钩子，
        # 未迁移/未知类型（judge/agent 等）沿用下方分支与默认回落，行为不变
        adapter = evaluator_registry.get(evaluation_type)
        if adapter is not None:
            aggregated = adapter.aggregate_task(task, results)
            if aggregated is not None:
                return aggregated

        # W17：rag 聚合已迁移注册表（见上方适配器查找）

        # 一致性评测走专用聚合
        if evaluation_type == "consistency":
            return ResultAggregator.aggregate_consistency_results(results)

        # W17：success_rate 聚合已迁移注册表（见上方适配器查找）

        # W17：latency 聚合已迁移注册表（threshold 取 task.latency_config 缺省 10.0，
        # 与本分支一致；见上方适配器查找）

        # W17：binary_classification 聚合已迁移注册表（见上方适配器查找）

        # W17：plugin（插件评测）聚合已迁移注册表（见上方适配器查找）

        # judge/agent 等默认走任务级聚合
        return ResultAggregator(task).aggregate(results)

    def aggregate(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合评测结果（新版本）

        Args:
            results: 评测结果列表

        Returns:
            聚合后的结果
        """
        if not results:
            return {
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "average_score": 0,
                "items": [],
            }

        # M2 QE2-4 金丝雀探针行整行剥离：不计入任何任务级统计（均分/行通过率/
        # 维度分/校验/轨迹/pass^k），单独聚合为 judge_governance.canary——
        # 探针是给裁判的对抗样本，混入均分会把「裁判被欺骗」摊薄成正常分
        canary_rows = [r for r in results if isinstance(r, dict) and r.get("judge_canary")]
        if canary_rows:
            results = [
                r for r in results
                if not (isinstance(r, dict) and r.get("judge_canary"))
            ]

        # 过滤成功的结果
        successful_results = [r for r in results if r.get("success", False)]
        failed_results = [r for r in results if not r.get("success", False)]

        # 计算统计信息
        total = len(results)
        success_count = len(successful_results)
        failed_count = len(failed_results)

        # 计算平均分（行级 average_score 为 None 的 judge_failed 行自动排除，W2-2）
        scores = [r.get("average_score", 0) for r in successful_results if r.get("average_score") is not None]
        average_score = sum(scores) / len(scores) if scores else 0

        # W2-2: 全部裁判失败的行计数（row_status=judge_failed，不进任务均分）
        failed_judge_rows = sum(1 for r in results if r.get("row_status") == "judge_failed")

        # 聚合各维度评分
        dimension_scores = self._aggregate_dimension_scores(successful_results)

        # 构建结果
        aggregated = {
            "total": total,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": success_count / total if total > 0 else 0,
            "average_score": round(average_score, 2),
            "max_score": self.max_score,
            "dimension_scores": dimension_scores,
            "failed_judge_rows": failed_judge_rows,
            "items": self._format_items(successful_results),
            "failed_items": self._format_failed_items(failed_results),
            "completed_at": datetime.now().isoformat(),
        }

        # 终态校验器聚合（M2 QE2-1）：有校验行的任务才写（无校验任务聚合结构
        # 不变，门禁引用 verification_pass_rate 仍按 metric 不可用 → error）
        verification_stats = self._aggregate_verification(results)
        if verification_stats is not None:
            aggregated["verification"] = verification_stats
            aggregated["verification_pass_rate"] = verification_stats["pass_rate"]

        # 轨迹匹配聚合（M2 QE2-2）：有匹配行的任务才写（口径与校验器聚合一致）
        trajectory_stats = self._aggregate_trajectory(results)
        if trajectory_stats is not None:
            aggregated["trajectory"] = trajectory_stats
            aggregated["trajectory_match_rate"] = trajectory_stats["pass_rate"]

        # pass^k 聚合（M2 QE2-3，judge 行级多 trial）：有携带 pass_at_1 的
        # 合并行才写（trials=1/存量任务聚合结构不变）
        pass_k_stats = aggregate_pass_k_rows(successful_results)
        if pass_k_stats is not None:
            aggregated["pass_k"] = pass_k_stats

        # 裁判治理聚合（M2 QE2-4）：金丝雀探针行单独成块（结果页裁判治理
        # 面板消费；无探针任务聚合结构不变）
        canary_stats = self._aggregate_canary(canary_rows)
        if canary_stats is not None:
            aggregated["judge_governance"] = {"canary": canary_stats}

        return aggregated

    def _aggregate_canary(self, canary_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """金丝雀探针行聚合（M2 QE2-4，空样本对抗自检）

        行口径：result.judge_canary（{kind, content}，执行器打标）；裁判分
        ≥ 满分 × CANARY_HIGH_SCORE_RATIO（0.7）即「高分」——空/乱码拿到
        高分说明裁判可被欺骗，risk=true（结果页风险徽标）。
        无探针行返回 None（聚合不写 judge_governance 键）。
        """
        if not canary_rows:
            return None
        from app.services.judge_canary import CANARY_HIGH_SCORE_RATIO

        threshold = round((self.max_score or 10) * CANARY_HIGH_SCORE_RATIO, 4)
        items = []
        high_score_count = 0
        for row in canary_rows:
            marker = row.get("judge_canary") if isinstance(row.get("judge_canary"), dict) else {}
            score = row.get("average_score")
            is_high = isinstance(score, (int, float)) and score >= threshold
            if is_high:
                high_score_count += 1
            items.append({
                "idx": _row_index(row, len(items)),
                "kind": marker.get("kind", ""),
                "success": bool(row.get("success")),
                "score": score,
                "high": is_high,
                "error": row.get("error", ""),
            })
        scored = [i for i in items if i["score"] is not None]
        return {
            "rows": len(items),
            "scored": len(scored),
            "high_score_count": high_score_count,
            "max_canary_score": max((i["score"] for i in scored), default=None),
            "threshold": threshold,
            # 任一探针高分即风险（宁可误报不可漏报——裁判治理面板主口径）
            "risk": high_score_count > 0,
            "items": items,
        }

    @staticmethod
    def _aggregate_verification(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """终态校验器任务级聚合（M2 QE2-1）

        行口径：result.verification_result.status（pass/fail/error）；
        分母为「已运行校验器的行」（verification_result 存在的行）——模型调用
        失败等未到校验步骤的行不计入（这些行已按 failed 计入 success_rate）。
        任务无任何校验行返回 None（聚合不写 verification 键）。
        """
        return _aggregate_status_rate(results, "verification_result")

    @staticmethod
    def _aggregate_trajectory(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """轨迹匹配任务级聚合（M2 QE2-2，口径与校验器聚合一致）

        行口径：result.trajectory_match.status（pass/fail/error）；分母为
        「已运行匹配的行」。任务无任何匹配行返回 None（聚合不写 trajectory 键，
        门禁引用 trajectory_match_rate 按 metric 不可用 → error）。
        """
        return _aggregate_status_rate(results, "trajectory_match")

    def _aggregate_dimension_scores(
        self,
        results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """聚合各维度评分"""
        dimension_stats = {}

        # 初始化维度统计
        for metric in self.evaluation_metrics:
            metric_name = metric.get("name", "")
            if metric_name:
                dimension_stats[metric_name] = {
                    "scores": [],
                    "weight": metric.get("weight", 1),
                }

        # 收集各维度评分
        for result in results:
            dim_scores = result.get("dimension_scores", {})
            for dim_name, dim_value in dim_scores.items():
                if dim_name in dimension_stats:
                    dimension_stats[dim_name]["scores"].append(dim_value)

        # 计算各维度平均分
        aggregated_dims = {}
        for dim_name, stats in dimension_stats.items():
            scores = stats["scores"]
            if scores:
                avg = sum(scores) / len(scores)
                aggregated_dims[dim_name] = {
                    "average": round(avg, 2),
                    "count": len(scores),
                    "weight": stats["weight"],
                }
            else:
                # 维度配置了但没有评分数据时，仍保留该维度并标记 average 为 None
                # 以便前端能够正确显示"无数据"而不是缺少维度条目
                aggregated_dims[dim_name] = {
                    "average": None,
                    "count": 0,
                    "weight": stats["weight"],
                }

        return aggregated_dims

    def format_result(self, task) -> Dict[str, Any]:
        """
        格式化评测结果（用于API返回）

        Args:
            task: 任务对象，包含 result 属性

        Returns:
            格式化后的结果字典
        """
        if not task or not task.result:
            return {
                "task_id": task.id if task else None,
                "status": task.status if task else None,
                "items": [],
            }

        result_data = task.result
        items = result_data.get("items", [])

        # 如果 items 是 JudgeEvaluationResult 对象列表，直接格式化
        formatted_items = []
        for item in items:
            if isinstance(item, JudgeEvaluationResult):
                formatted_items.append({
                    "question_index": item.question_index,
                    "question": item.question,
                    "ref_answer": item.ref_answer,
                    "model_answer": item.model_answer,
                    "overall_score": item.overall_score,
                    "success": item.success,
                    "user_input": item.user_input,
                    "duration": item.duration,
                    "scores": item.scores,
                    "reasoning": item.reasoning,
                    "error": item.error,
                })
            elif isinstance(item, dict):
                formatted_items.append(item)
            else:
                formatted_items.append({"error": f"Unknown item type: {type(item)}"})

        return {
            "task_id": task.id,
            "status": task.status,
            "items": formatted_items,
        }

    def _format_items(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """格式化成功结果"""
        items = []
        # 结构化字段（已单独处理的字段）
        structured_keys = {
            "idx", "success", "judge_scores", "average_score", "model_output",
            "feedback", "dimension_scores", "input_data", "model_raw_response",
            "pre_script_results", "row_status", "judge_count", "failed_judge_count",
            "verification_result", "trajectory_match",
            # 行级多 trial 字段（M2 QE2-3）：下方显式写入，避免平铺重复
            "trials_count", "trials_detail", "trial_scores", "trial_passes",
            "pass_at_1", "pass_pow_k", "pass_count", "trials",
            "score_mean", "score_std", "score_sem",
        }

        for idx, result in enumerate(results):
            judge_scores = result.get("judge_scores", [])
            item = {
                # 行序号优先取 _orig_idx（行级表重建行的事实源序号，历史 row_result
                # JSON 里 idx 可能全为 0），无行身份再回退 idx / 枚举序号
                "idx": _row_index(result, idx),
                "success": True,
                "model_output": result.get("model_output", ""),
                "model_raw_response": result.get("model_raw_response", ""),
                "input_data": result.get("input_data", ""),
                "judge_count": len(judge_scores),
                "judge_scores": judge_scores,
                "judge_details": [
                    {
                        "judge_index": i,
                        "score": js.get("score"),
                        "feedback": js.get("feedback"),
                        "dimension_scores": js.get("dimension_scores"),
                        # 分维度评分理由（新裁判输出才有；旧数据缺该键时为 None）
                        "dimension_feedbacks": js.get("dimension_feedbacks"),
                        "evaluation_target_name": js.get("evaluation_target_name") or f"裁判{i+1}",
                        "langfuse_trace_id": js.get("langfuse_trace_id"),
                        "langfuse_trace_url": js.get("langfuse_trace_url"),
                        # W2-2 增量：失败裁判标记（旧消费方忽略未知字段不受影响）
                        "judge_failed": bool(js.get("judge_failed")),
                        "fail_reason": js.get("fail_reason"),
                    }
                    for i, js in enumerate(judge_scores)
                ] if judge_scores else [],
                "feedback": result.get("feedback", ""),
                "dimension_scores": result.get("dimension_scores", {}),
            }
            # W2-2 增量：行级评分口径字段（completed / judge_failed / empty_input）
            item["row_status"] = result.get("row_status", "completed")
            item["failed_judge_count"] = result.get("failed_judge_count", 0)
            # average_score 为 None（judge_failed 行）时省略该字段：
            # 前端 Results.vue 对 null 会调 toFixed 抛错，缺 key 走 undefined 分支安全
            avg_score = result.get("average_score", 0)
            if avg_score is not None:
                item["average_score"] = avg_score
            # 前处理脚本结果全量入库（设计文档 §三 结果展示通道 / §六 结果结构），
            # judge 与 agent 两种评测类型均生效
            if "pre_script_results" in result:
                item["pre_script_results"] = result["pre_script_results"]
            # 终态校验器结果（M2 QE2-1）：证据块展示用，judge 与 agent 均生效；
            # 历史/未配置校验器的结果无该键，不写入（消费方按缺键跳过）
            if "verification_result" in result:
                item["verification_result"] = result["verification_result"]
            # 轨迹匹配结果（M2 QE2-2）：轨迹时间线展示用，agent 类型生效；
            # 历史/未配置轨迹断言的结果无该键，不写入
            if "trajectory_match" in result:
                item["trajectory_match"] = result["trajectory_match"]
            # 行级多 trial 统计（M2 QE2-3，judge trials>1）：pass@1/pass^k 徽标
            # 与 trial 明细展示用；trials=1/历史结果无该组键，不写入
            if "trials_count" in result:
                for key in ("trials_count", "trials_detail", "trial_scores",
                            "trial_passes", "pass_at_1", "pass_pow_k",
                            "pass_count", "trials", "score_mean", "score_std",
                            "score_sem"):
                    if key in result:
                        item[key] = result[key]
            # 平铺输入变量（排除结构化字段、内部字段和空值字段）
            for k, v in result.items():
                if k not in structured_keys and not k.startswith("_") and v is not None:
                    item[k] = v
            items.append(item)

        return items

    def _format_failed_items(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """格式化失败结果（携带输入上下文与模型输出，便于结果页定位失败原因）"""
        # 结构化字段（已单独处理或无需平铺的字段），平铺口径与 _format_items 一致：
        # 排除下划线开头键与值为 None 的键
        structured_keys = {
            "idx", "success", "error", "model_output", "model_raw_response",
            "judge_scores", "judge_details", "average_score", "feedback",
            "dimension_scores", "input_data", "row_status", "pre_script_results",
            "judge_count", "failed_judge_count", "verification_result",
            "trajectory_match",
        }
        items = []

        for idx, result in enumerate(results):
            item = {
                # 与 _format_items 同口径：优先 _orig_idx（行级表事实源序号）
                "idx": _row_index(result, idx),
                "success": False,
                "error": result.get("error", "Unknown error"),
                # or "" 兜底：键存在但值为 None（如 judge_failed 行）时按空串处理
                "model_output": result.get("model_output") or "",
                # 失败行无 row_status 时按 derive_row_status 口径默认 failed
                "row_status": result.get("row_status") or "failed",
            }
            # on_fail=abort 失败的条目保留脚本结果，便于定位失败脚本
            if result.get("pre_script_results"):
                item["pre_script_results"] = result["pre_script_results"]
            # 终态校验器 abort 失败的条目保留校验证据（M2 QE2-1）
            if result.get("verification_result"):
                item["verification_result"] = result["verification_result"]
            # 失败条目保留轨迹匹配证据（M2 QE2-2）
            if result.get("trajectory_match"):
                item["trajectory_match"] = result["trajectory_match"]
            # 平铺输入变量（与 _format_items 同口径：排除结构化字段、下划线开头与 None 值）
            for k, v in result.items():
                if k not in structured_keys and not k.startswith("_") and v is not None:
                    item[k] = v
            items.append(item)

        return items
    def aggregate_judge_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合裁判评测结果

        Args:
            results: 评测结果列表

        Returns:
            聚合后的结果
        """
        if not results:
            return {
                "total_questions": 0,
                "average_score": 0.0,
                "score_distribution": {},
            }

        # 计算平均分
        scores = [r.get("overall_score", 0) for r in results if r.get("success")]
        average_score = sum(scores) / len(scores) if scores else 0.0

        # 分数分布
        score_distribution = {
            "0-20": 0,
            "21-40": 0,
            "41-60": 0,
            "61-80": 0,
            "81-100": 0,
        }
        for score in scores:
            if score <= 20:
                score_distribution["0-20"] += 1
            elif score <= 40:
                score_distribution["21-40"] += 1
            elif score <= 60:
                score_distribution["41-60"] += 1
            elif score <= 80:
                score_distribution["61-80"] += 1
            else:
                score_distribution["81-100"] += 1

        return {
            "total_questions": len(results),
            "successful_questions": len(scores),
            "average_score": round(average_score, 2),
            "min_score": min(scores) if scores else 0,
            "max_score": max(scores) if scores else 0,
            "score_distribution": score_distribution,
        }

    @staticmethod
    def aggregate_consistency_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合一致性评测结果

        Args:
            results: 一致性评测结果列表

        Returns:
            聚合后的结果，包含汇总指标和详细数据
        """
        if not results:
            return {
                "total_questions": 0,
                "average_text_similarity": 0.0,
                "average_format_consistency": 0.0,
                "average_structure_consistency": 0.0,
                "items": [],
                "custom_dimension_summary": {},
                "dimension_scores": {},
            }

        # 提取一致性指标
        text_similarities = []
        format_consistencies = []
        structure_consistencies = []
        detailed_items = []

        # 用于聚合所有维度评分（固定维度+自定义维度）
        all_dimension_scores: Dict[str, List[float]] = {}

        for r in results:
            metrics = r.get("metrics", {}) or {}
            text_similarities.append(metrics.get("text_similarity", 0))
            format_consistencies.append(metrics.get("format_consistency", 0))
            structure_consistencies.append(metrics.get("structure_consistency", 0))

            # 保留详细结果
            repeat_results = r.get("repeat_results", []) or []

            # 获取待评测内容或模型输出
            evaluation_content = r.get("evaluation_content", "")
            model_output = evaluation_content

            # 如果没有待评测内容，取第一次成功的模型输出
            if not model_output:
                for rr in repeat_results:
                    if rr.get("success") and rr.get("result"):
                        model_output = rr.get("result", "")
                        break

            # 如果模型输出为空，显示"暂无输出"
            if not model_output:
                model_output = "暂无输出"

            # 获取裁判评估结果
            judge_assessment = r.get("judge_assessment", {}) or {}

            # 解析各维度评分（包含固定维度和自定义维度）
            dimension_scores = {}
            custom_dimensions = {}
            all_dims = judge_assessment.get("各维度评分", {}) or {}

            for dim_name, dim_data in all_dims.items():
                if isinstance(dim_data, dict) and "score" in dim_data:
                    score = dim_data["score"]
                    dimension_scores[dim_name] = score
                    # 收集用于聚合
                    all_dimension_scores.setdefault(dim_name, []).append(score)
                elif isinstance(dim_data, (int, float)):
                    # 兜底：直接是数字的维度评分
                    score = float(dim_data)
                    dimension_scores[dim_name] = score
                    all_dimension_scores.setdefault(dim_name, []).append(score)

            detailed_items.append({
                "idx": r.get("idx", 0),
                "success": r.get("success", False),
                "model_output": model_output,
                "evaluation_content": evaluation_content,  # 待评测内容
                "metrics": {
                    "text_similarity": metrics.get("text_similarity", 0),
                    "format_consistency": metrics.get("format_consistency", 0),
                    "structure_consistency": metrics.get("structure_consistency", 0),
                    "unique_result_count": metrics.get("unique_result_count", 0),
                    "total_calls": metrics.get("total_calls", 0),
                    "successful_calls": metrics.get("successful_calls", 0),
                    # pass^k 行级口径（M2 QE2-3）：旧结果无该组键时取 None
                    # （前端 pass^k 徽标按缺键跳过）
                    "pass_at_1": metrics.get("pass_at_1"),
                    "pass_pow_k": metrics.get("pass_pow_k"),
                    "trials": metrics.get("trials"),
                    "pass_count": metrics.get("pass_count"),
                    "score_std": metrics.get("score_std"),
                    "score_sem": metrics.get("score_sem"),
                },
                "judge_assessment": judge_assessment,
                "dimension_scores": dimension_scores,  # 各维度评分（固定+自定义）
                "custom_dimensions": custom_dimensions,  # 自定义维度评分
                "repeat_results": repeat_results,
                "error": r.get("error", ""),
                # 平铺输入变量
                **{k: v for k, v in r.items()
                   if k not in ("idx", "success", "metrics", "repeat_results",
                                "judge_assessment", "evaluation_content", "error")
                   and not k.startswith("_")},
            })

        # 计算所有维度的平均分
        aggregated_dims = {}
        for dim_name, scores in all_dimension_scores.items():
            aggregated_dims[dim_name] = {
                "average": round(sum(scores) / len(scores), 2),
                "count": len(scores),
            }

        # pass^k 聚合（M2 QE2-3）：行携带 pass_at_1（repeat 次数的通过统计）
        # 才写；旧版一致性结果无该键，聚合结构不变
        aggregated_pass_k = aggregate_pass_k_rows(results)

        result = {
            "total_questions": len(results),
            "average_text_similarity": round(sum(text_similarities) / len(text_similarities), 4) if text_similarities else 0,
            "average_format_consistency": round(sum(format_consistencies) / len(format_consistencies), 4) if format_consistencies else 0,
            "average_structure_consistency": round(sum(structure_consistencies) / len(structure_consistencies), 4) if structure_consistencies else 0,
            "items": detailed_items,
            "custom_dimension_summary": aggregated_dims,  # 保持向后兼容
            "dimension_scores": aggregated_dims,  # 统一的维度评分汇总
        }
        if aggregated_pass_k is not None:
            result["pass_k"] = aggregated_pass_k
            # 顶层便捷口径（结果卡片直读，嵌套块保留全量明细）
            result["average_pass_at_1"] = aggregated_pass_k["average_pass_at_1"]
            result["average_pass_pow_k"] = aggregated_pass_k["average_pass_pow_k"]
        return result

    @staticmethod
    def aggregate_success_rate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合成功率评测结果

        Args:
            results: 成功率评测结果列表

        Returns:
            聚合后的结果
        """
        if not results:
            return {
                "total_questions": 0,
                "overall_success_rate": 0.0,
            }

        # 提取成功率
        success_rates = []
        total_calls = 0
        total_successes = 0

        for r in results:
            metrics = r.get("metrics", {})
            success_rates.append(metrics.get("success_rate", 0))
            total_calls += metrics.get("total_calls", 0)
            total_successes += metrics.get("successful_calls", 0)

        return {
            "total_questions": len(results),
            "overall_success_rate": round(total_successes / total_calls, 4) if total_calls > 0 else 0,
            "average_success_rate": round(sum(success_rates) / len(success_rates), 4) if success_rates else 0,
            "total_calls": total_calls,
            "total_successes": total_successes,
            # pass^k 聚合（M2 QE2-3）：行携带 pass_at_1 才写（旧版结果无该键，
            # 聚合结构不变）；pass@1 与 overall_success_rate 互为校验
            **({"pass_k": pass_k_stats} if (pass_k_stats := aggregate_pass_k_rows(results)) is not None else {}),
        }


    @staticmethod
    def aggregate_plugin_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合插件评测结果（每行一个插件/skill，行分为复合分 0-100）

        trace 口径（plugin_eval.mode == "trace"）：一行一份合并报告 —— 行 ``average_score``
        与其余来源一致表示综合分（静态 + LLM 评审），真实数据分单独汇总为 ``real_data_summary``，
        两套互不并入；trace 另出 ``composite_summary``（综合分侧的行数与均分）。

        Args:
            results: 插件评测行结果列表

        Returns:
            聚合后的结果：平均复合分、徽章分布、维度均分、反模式汇总、逐项明细
            （trace 口径另出 mode、real_data_summary 与 composite_summary）
        """
        if not results:
            return {
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "average_score": 0,
                "badge_summary": {},
                "dimension_scores": {},
                "anti_pattern_summary": {},
                "market": None,
                "evaluator": None,
                "mode": None,
                "real_data_summary": None,
                "composite_summary": None,
                "design_quality": None,
                "items": [],
            }

        successful_results = [r for r in results if r.get("success", False)]
        failed_results = [r for r in results if not r.get("success", False)]

        # 口径判定：任一行来自 trace（历史/真实数据）即按 trace 聚合
        # （行级 evaluation_mode 优先：全行失败时 plugin_eval 已置空，仍需按 trace 渲染）
        mode = "trace" if any(
            r.get("evaluation_mode") == "trace"
            or (r.get("plugin_eval") or {}).get("mode") == "trace"
            for r in results
        ) else None

        # 设计质量线按插件只评一次：明细/徽章/维度/反模式的「设计侧」汇总必须按插件去重，
        # 否则同一插件 N 行会把同一份结果重复计 N 次
        design_once = mode == "trace" and any(
            (r.get("plugin_eval") or {}).get("design") for r in successful_results
        )
        design_rows: List[Dict[str, Any]] = []
        if design_once:
            seen_plugins = set()
            for r in successful_results:
                key = str(r.get("target_name") or "")
                if key in seen_plugins:
                    continue
                seen_plugins.add(key)
                design_rows.append(r)
        else:
            design_rows = list(successful_results)

        # 平均复合分（0-100；失败行 average_score 为 None 自动排除）
        scores = [r.get("average_score") for r in design_rows if r.get("average_score") is not None]
        average_score = sum(scores) / len(scores) if scores else 0

        # 徽章分布（trace 旧口径的行不产徽章，只统计真有综合分的行，避免「全 no_badge」的误导分布）
        badge_summary: Dict[str, int] = {}
        for r in design_rows:
            if mode == "trace" and not (r.get("plugin_eval") or {}).get("composite"):
                continue
            badge = r.get("badge") or "no_badge"
            badge_summary[badge] = badge_summary.get(badge, 0) + 1

        # 维度均分（0-1；未测维度 None 自动排除）
        dimension_acc: Dict[str, List[float]] = {}
        for r in design_rows:
            for dim_name, dim_score in (r.get("dimension_scores") or {}).items():
                if isinstance(dim_score, (int, float)):
                    dimension_acc.setdefault(dim_name, []).append(float(dim_score))
        dimension_scores = {
            dim: {
                "average": round(sum(vals) / len(vals), 4),
                "count": len(vals),
            }
            for dim, vals in dimension_acc.items()
        }

        # 反模式汇总（同一 flag 计数 + 最重严重度 + 首条描述）
        anti_pattern_summary: Dict[str, Dict[str, Any]] = {}
        for r in design_rows:
            for ap in r.get("anti_patterns") or []:
                flag = ap.get("flag", "UNKNOWN")
                entry = anti_pattern_summary.setdefault(
                    flag,
                    {"count": 0, "max_severity": 0.0, "description": ap.get("description", "")},
                )
                entry["count"] += 1
                entry["max_severity"] = max(entry["max_severity"], float(ap.get("severity", 0) or 0))

        # 逐项明细（供前端与导出复用）
        items = []
        for r in successful_results:
            eval_detail = r.get("plugin_eval") or {}
            composite = eval_detail.get("composite") or {}
            item = {
                "idx": r.get("idx", 0),
                "target_name": r.get("target_name", ""),
                "target_type": r.get("target_type", ""),
                "score": r.get("average_score"),
                "badge": r.get("badge", "no_badge"),
                "confidence_label": composite.get("confidence_label", ""),
                "depth": eval_detail.get("depth", ""),
                "duration_ms": eval_detail.get("duration_ms"),
                "anti_pattern_penalty": composite.get("anti_pattern_penalty"),
                "dimension_scores": r.get("dimension_scores") or {},
                "dimensions": composite.get("dimensions", []),
                "anti_patterns": r.get("anti_patterns") or [],
                "market": r.get("market_info") or None,
                "evaluator": (r.get("plugin_eval") or {}).get("evaluator") or None,
                "layers": [
                    {
                        "layer": lr.get("layer"),
                        "score": lr.get("score"),
                        "sub_scores": lr.get("sub_scores"),
                    }
                    for lr in eval_detail.get("layers", [])
                ],
                "warnings": eval_detail.get("warnings", []),
                "model_usage": eval_detail.get("model_usage", {}),
                # trace 口径专属：真实数据分与线程锚点（非 trace 行恒为 None/空）
                "real_data": r.get("real_data") or None,
                "trace": r.get("trace") or None,
                "checks": r.get("checks") or [],
                "checks_total": r.get("checks_total"),
                "unmeasured_reason": (r.get("trace") or {}).get("unavailable_reason") or "",
                # 两套分数的显式取值（trace 行才加：旧落库记录没有这两个键，
                # 前端据此区分「旧口径行分即真实数据分」与「本次未出综合分」）
                **(
                    {
                        # trace 行的 average_score 即综合分（没出综合分时为 None）
                        "composite_score": (
                            r.get("average_score") if composite.get("score") is not None else None
                        ),
                        "real_data_score": (r.get("real_data") or {}).get("score"),
                        "skills": eval_detail.get("skills") or [],
                        # 设计质量线：是否复用任务内首次评测（页面据此标注）
                        "design_reused": bool(r.get("design_reused")),
                        "design": eval_detail.get("design") or None,
                    }
                    if mode == "trace"
                    else {}
                ),
            }
            items.append(item)
        failed_items = [
            {
                "idx": r.get("idx", 0),
                "success": False,
                "error": r.get("error", ""),
            }
            for r in failed_results
        ]

        # 市场来源的版本锚点：全部行指向同一市场同一 commit 时提到顶层，
        # 便于直接回答「这次评的是哪个版本的插件」（manual 来源为 None）
        market_rows = [i["market"] for i in items if i.get("market")]
        market = None
        if market_rows and len({(m.get("market_id"), m.get("commit")) for m in market_rows}) == 1:
            market = market_rows[0]

        # 评测动作自身的版本锚点（plugin-eval 也来自市场，随懒加载更新）
        evaluator_rows = [i["evaluator"] for i in items if i.get("evaluator")]
        evaluator = evaluator_rows[0] if evaluator_rows else None

        real_data_summary = ResultAggregator._summarize_real_data(successful_results, mode)
        composite_summary = ResultAggregator._summarize_composite(successful_results, mode)
        design_quality = ResultAggregator._summarize_design_quality(successful_results, mode)

        return {
            "total": len(results),
            "success_count": len(successful_results),
            "failed_count": len(failed_results),
            "success_rate": len(successful_results) / len(results) if results else 0,
            "average_score": round(average_score, 2),
            "badge_summary": badge_summary,
            "dimension_scores": dimension_scores,
            "anti_pattern_summary": anti_pattern_summary,
            "market": market,
            "evaluator": evaluator,
            "mode": mode,
            "real_data_summary": real_data_summary,
            "composite_summary": composite_summary,
            "design_quality": design_quality,
            "items": items,
            "failed_items": failed_items,
            "completed_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _summarize_composite(
        successful_results: List[Dict[str, Any]], mode: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """trace 口径的设计质量线汇总（非 trace 返回 None）

        ``average_score`` 即综合分。设计质量线按插件只评一次：任务内同一插件无论覆盖
        多少行，都只算一个样本（``unit="plugin"``）；改动前的旧记录没有 ``design`` 标记，
        仍按行统计（``unit="row"``），保证历史结果不变。
        """
        if mode != "trace":
            return None

        design_once = any(
            (r.get("plugin_eval") or {}).get("design") for r in successful_results
        )
        scores: List[float] = []
        seen = set()
        for r in successful_results:
            composite = (r.get("plugin_eval") or {}).get("composite") or {}
            score = composite.get("score")
            if not isinstance(score, (int, float)):
                continue
            if design_once:
                key = str(r.get("target_name") or "")
                if key in seen:
                    continue
                seen.add(key)
            scores.append(float(score))
        if design_once:
            names = {
                str(r.get("target_name") or "")
                for r in successful_results
                if str(r.get("target_name") or "")
            }
            total = len(names) if names else (1 if scores else 0)
        else:
            total = len(successful_results)
        return {
            "average_score": round(sum(scores) / len(scores), 2) if scores else None,
            "measured_count": len(scores),
            "unmeasured_count": max(0, total - len(scores)),
            "unit": "plugin" if design_once else "row",
        }

    @staticmethod
    def _summarize_design_quality(
        successful_results: List[Dict[str, Any]], mode: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """设计质量线明细（按插件一条，任务内只评一次）

        页面据此单独渲染「设计质量线」：综合分/徽章/维度/各层/Skill 明细/模型用量，
        并给出该插件覆盖了多少行、其中多少行复用了首次评测。
        """
        if mode != "trace":
            return None

        plugins: Dict[str, Dict[str, Any]] = {}
        for r in successful_results:
            ev = r.get("plugin_eval") or {}
            composite = ev.get("composite") or {}
            score = composite.get("score")
            name = str(r.get("target_name") or "")
            entry = plugins.get(name)
            if entry is None:
                entry = plugins[name] = {
                    "plugin": name,
                    "score": None,
                    "badge": "",
                    "confidence_label": "",
                    "anti_pattern_penalty": None,
                    "dimensions": [],
                    "layers": [],
                    "skills": [],
                    "depth": ev.get("depth") or "",
                    "model_usage": {},
                    "duration_ms": None,
                    "covered_rows": 0,
                    "reused_rows": 0,
                    "anti_pattern_count": 0,
                    "evaluator": ev.get("evaluator") or None,
                }
            entry["covered_rows"] += 1
            entry["anti_pattern_count"] = max(
                entry["anti_pattern_count"], len(r.get("anti_patterns") or [])
            )
            if r.get("design_reused"):
                entry["reused_rows"] += 1
            if entry["score"] is None and isinstance(score, (int, float)):
                design = ev.get("design") or {}
                entry.update({
                    "score": round(float(score), 2),
                    "badge": ev.get("badge") or composite.get("badge") or "",
                    "confidence_label": composite.get("confidence_label") or "",
                    "anti_pattern_penalty": composite.get("anti_pattern_penalty"),
                    "dimensions": composite.get("dimensions") or [],
                    "layers": ev.get("layers") or [],
                    "skills": ev.get("skills") or [],
                    "depth": ev.get("depth") or "",
                    "model_usage": design.get("model_usage") or {},
                    "duration_ms": design.get("duration_ms"),
                })
        return {
            "evaluated_count": sum(1 for p in plugins.values() if p["score"] is not None),
            "plugins": [
                p for name, p in plugins.items() if name or p["score"] is not None
            ],
        }

    @staticmethod
    def _summarize_real_data(
        successful_results: List[Dict[str, Any]], mode: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """trace 口径的真实数据分汇总（非 trace 返回 None，保持既有结果结构不变）

        - average_score：已测行的真实数据分均值（未测行 score 为 None 自动排除）
        - unmeasured_count：内容不足以判定的行数（无原始会话 / 无规则 / 未识别）
        - check_summary：逐行判定计数求和（通过/失败/证据不足/不适用）
        - average_behavior_coverage：已测行的关键步骤覆盖率均值
        - score_label：得分标签。plugin-eval 对「评审不完整」的线程一律标「部分样本得分」
          （report.md: `部分样本得分` vs `真实数据得分`），有任一行未评审完整即按部分样本标；
        - decidable_count / applicable_count：得分下方那一行必须同屏的样本量——
          有效判定分母只有「通过 + 失败」，证据不足的检查不进分母，因此得分高
          只代表「已判定的少量样本里没有违规」，不代表整条线程；
        - conservative_score：把证据不足的检查一并计入违规时的下界（敏感性对照，
          不是 plugin-eval 指标；plugin-eval 的口径是 通过 ÷ (通过 + 失败)）；
        - direction_summary：方向（报告「分项」表）逐行求和后的可判定/适用与均值。
          **只统计已探测到任务的阶段**：未测阶段的检查集为空，若把它的方向也拼进来，
          页面会出现整行 0/0 的假分项（与报告「两阶段各一节」的结构不符）。
        - phase_summary：逐阶段汇总（报告「测评总览」的生成/执行两行），未测阶段保留
          status=unmeasured 与空计数，页面据此显示「未测」而不是 0。
        - behavior_scope：覆盖率口径（key_steps=关键步骤 / all=全部行为），列名随它切换。
        """
        if mode != "trace":
            return None

        scores: List[float] = []
        coverages: List[float] = []
        behaviors: List[float] = []
        status_summary: Dict[str, int] = {}
        partial = False
        check_summary = {
            "passed": 0, "failed": 0, "insufficient": 0, "not_applicable": 0, "measured": 0,
        }
        # 方向 → {label, phase, 分数列表, 有效率列表, measured/applicable 求和}
        direction_acc: Dict[str, Dict[str, Any]] = {}
        # 阶段 → 汇总（未测阶段也保留，页面显示未测而非 0）
        phase_acc: Dict[str, Dict[str, Any]] = {}
        behavior_scopes = set()
        for r in successful_results:
            real_data = r.get("real_data") or {}
            status = str(real_data.get("status") or "unmeasured")
            status_summary[status] = status_summary.get(status, 0) + 1
            if status != "complete" or real_data.get("extraction_complete") is False:
                partial = True
            score = real_data.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
            coverage = real_data.get("coverage")
            if isinstance(coverage, (int, float)):
                coverages.append(float(coverage))
            behavior = real_data.get("behavior_coverage") or {}
            behavior_rate = behavior.get("rate")
            if isinstance(behavior_rate, (int, float)):
                behaviors.append(float(behavior_rate))
            if behavior.get("scope"):
                behavior_scopes.add(str(behavior["scope"]))
            for key in check_summary:
                value = (real_data.get("counts") or {}).get(key)
                if isinstance(value, (int, float)):
                    check_summary[key] += int(value)
            for phase in (r.get("trace") or {}).get("phases") or []:
                metric = str(phase.get("metric") or phase.get("label") or "")
                phase_row = phase_acc.setdefault(
                    metric,
                    {
                        "metric": metric,
                        "label": phase.get("label") or metric,
                        "status": phase.get("status") or "unmeasured",
                        "detected": phase.get("detected"),
                        "scores": [],
                        "coverages": [],
                        "decidable": 0,
                        "applicable": 0,
                        "measured_rows": 0,
                        "row_count": 0,
                    },
                )
                phase_row["row_count"] += 1
                if phase.get("status") != "unmeasured":
                    phase_row["status"] = phase.get("status")
                if phase.get("detected") is True:
                    phase_row["detected"] = True
                if isinstance(phase.get("score"), (int, float)):
                    phase_row["scores"].append(float(phase["score"]))
                    phase_row["measured_rows"] += 1
                if isinstance(phase.get("coverage"), (int, float)):
                    phase_row["coverages"].append(float(phase["coverage"]))
                counts = phase.get("counts") or {}
                measured = counts.get("measured")
                if not isinstance(measured, (int, float)):
                    measured = int(counts.get("passed", 0) or 0) + int(counts.get("failed", 0) or 0)
                applicable = counts.get("applicable")
                if not isinstance(applicable, (int, float)):
                    applicable = int(measured) + int(counts.get("insufficient", 0) or 0)
                phase_row["decidable"] += int(measured)
                phase_row["applicable"] += int(applicable)
                # 未测阶段没有检查项：不参与方向分项（否则整行 0/0）
                if phase.get("status") == "unmeasured" or phase.get("detected") is False:
                    continue
                for dim in phase.get("dimensions") or []:
                    row = direction_acc.setdefault(
                        dim.get("key") or dim.get("label") or "",
                        {"label": dim.get("label") or dim.get("key") or "",
                         "phase": phase.get("label") or metric,
                         "phase_metric": metric,
                         "phase_status": phase.get("status") or "",
                         "scores": [], "coverages": [], "measured": 0, "applicable": 0},
                    )
                    if isinstance(dim.get("score"), (int, float)):
                        row["scores"].append(float(dim["score"]))
                    if isinstance(dim.get("coverage"), (int, float)):
                        row["coverages"].append(float(dim["coverage"]))
                    row["measured"] += int(dim.get("measured") or 0)
                    row["applicable"] += int(dim.get("applicable") or 0)

        def _mean(values: List[float]) -> Optional[float]:
            return round(sum(values) / len(values), 2) if values else None

        decidable = check_summary["measured"] or (
            check_summary["passed"] + check_summary["failed"]
        )
        applicable = decidable + check_summary["insufficient"]

        return {
            "average_score": _mean(scores),
            "score_label": "部分样本得分" if partial else "真实数据得分",
            "decidable_count": decidable,
            "applicable_count": applicable,
            # 敏感性下界：证据不足的检查全部按违规计的对照值（非 plugin-eval 指标）
            "conservative_score": (
                round(check_summary["passed"] / applicable * 100, 2) if applicable else None
            ),
            "measured_count": len(scores),
            "unmeasured_count": len(successful_results) - len(scores),
            "status_summary": status_summary,
            "average_coverage": _mean(coverages),
            "average_behavior_coverage": _mean(behaviors),
            "behavior_scope": (
                behavior_scopes.pop() if len(behavior_scopes) == 1 else
                ("mixed" if behavior_scopes else "")
            ),
            "check_summary": check_summary,
            "phase_summary": [
                {
                    "metric": row["metric"],
                    "label": row["label"],
                    "status": row["status"],
                    "detected": row["detected"],
                    "average_score": _mean(row["scores"]),
                    "average_coverage": _mean(row["coverages"]),
                    "measured_rows": row["measured_rows"],
                    "row_count": row["row_count"],
                    "decidable": row["decidable"],
                    "applicable": row["applicable"],
                }
                for row in phase_acc.values()
            ],
            "direction_summary": [
                {
                    "key": key,
                    "label": row["label"],
                    "phase": row["phase"],
                    "phase_metric": row["phase_metric"],
                    "phase_status": row["phase_status"],
                    "average_score": _mean(row["scores"]),
                    "average_coverage": _mean(row["coverages"]),
                    "measured": row["measured"],
                    "applicable": row["applicable"],
                }
                for key, row in direction_acc.items()
            ],
        }

    @staticmethod
    def aggregate_binary_classification_results(
        results: List[Dict[str, Any]],
        metrics_config: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        聚合二分分类评测结果

        Args:
            results: 二分分类评测结果列表，每个元素包含 expected_label, predicted_label, is_correct
            metrics_config: 保留兼容，不再使用

        Returns:
            聚合结果，包含混淆矩阵及所有衍生指标
        """
        if not results:
            return {
                "total_count": 0,
                "confusion_matrix": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
                "accuracy": 0.0,
                "specificity": 0.0,
                "sensitivity": 0.0,
                "precision": 0.0,
                "f1_score": 0.0,
                "f2_score": 0.0,
                "f05_score": 0.0,
                "mcc": 0.0,
                "npv": 0.0,
                "error_rate": 0.0,
                "prevalence": 0.0,
                "youden_index": 0.0,
                "items": []
            }

        tp = fp = tn = fn = 0
        for r in results:
            expected = r.get("expected_label", False)
            predicted = r.get("predicted_label", False)
            if expected and predicted:
                tp += 1
            elif expected and not predicted:
                fn += 1
            elif not expected and predicted:
                fp += 1
            else:
                tn += 1

        total_count = len(results)

        # 基础指标
        accuracy = (tp + tn) / total_count if total_count > 0 else 0.0
        sensitivity = tp / (tp + fn) if (tp + fn) > 0 else 0.0  # Recall
        specificity = tn / (tn + fp) if (tn + fp) > 0 else 0.0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0

        # F 系列指标
        if (precision + sensitivity) > 0:
            f1_score = 2 * precision * sensitivity / (precision + sensitivity)
        else:
            f1_score = 0.0

        # F2: β=2, 偏重召回率
        beta2 = 4  # β²
        if (beta2 * precision + sensitivity) > 0:
            f2_score = (1 + beta2) * precision * sensitivity / (beta2 * precision + sensitivity)
        else:
            f2_score = 0.0

        # F0.5: β=0.5, 偏重精确率
        beta05 = 0.25  # β²
        if (beta05 * precision + sensitivity) > 0:
            f05_score = (1 + beta05) * precision * sensitivity / (beta05 * precision + sensitivity)
        else:
            f05_score = 0.0

        # Matthews 相关系数
        mcc_denom = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
        if mcc_denom > 0:
            mcc = (tp * tn - fp * fn) / (mcc_denom ** 0.5)
        else:
            mcc = 0.0

        # 阴性预测值
        npv = tn / (tn + fn) if (tn + fn) > 0 else 0.0

        # 错误率
        error_rate = 1 - accuracy

        # 阳性率 (正例实际占比)
        prevalence = (tp + fn) / total_count if total_count > 0 else 0.0

        # Youden 指数
        youden_index = sensitivity + specificity - 1

        return {
            "total_count": total_count,
            "confusion_matrix": {
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn
            },
            "accuracy": round(accuracy, 4),
            "specificity": round(specificity, 4),
            "sensitivity": round(sensitivity, 4),
            "precision": round(precision, 4),
            "f1_score": round(f1_score, 4),
            "f2_score": round(f2_score, 4),
            "f05_score": round(f05_score, 4),
            "mcc": round(mcc, 4),
            "npv": round(npv, 4),
            "error_rate": round(error_rate, 4),
            "prevalence": round(prevalence, 4),
            "youden_index": round(youden_index, 4),
            "items": results
        }


    @staticmethod
    def aggregate_rag_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合 RAG 检索质量评测结果

        Args:
            results: RAG 评测结果列表，每个元素包含
                     {"success": bool, "scores": {"context_precision": float, ...}, ...}

        Returns:
            聚合结果，包含 total_questions, successful_questions, metrics_summary, detail_results
        """
        if not results:
            return {
                "total_questions": 0,
                "successful_questions": 0,
                "metrics_summary": {},
                "detail_results": [],
                "completed_at": datetime.now().isoformat(),
            }

        successful = [r for r in results if r.get("success", False)]

        # 按指标聚合 average/min/max
        metric_values: Dict[str, List[float]] = {}
        for r in successful:
            for metric, value in (r.get("scores") or {}).items():
                if isinstance(value, (int, float)):
                    metric_values.setdefault(metric, []).append(float(value))

        metrics_summary = {}
        for metric, values in metric_values.items():
            metrics_summary[metric] = {
                "average": round(sum(values) / len(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "count": len(values),
            }

        # 构建明细结果
        detail_results = []
        for r in results:
            item_data = {
                "idx": r.get("idx", 0),
                "success": r.get("success", False),
                "contexts_count": len(r.get("contexts", [])),
                "scores": r.get("scores", {}),
                "reasons": r.get("reasons", {}),
                "error": r.get("error", ""),
            }
            # 平铺输入变量（排除结构化字段）
            for k, v in r.items():
                if k not in ("idx", "success", "contexts", "scores", "reasons",
                             "error", "model_answer") and not k.startswith("_"):
                    item_data[k] = v
            detail_results.append(item_data)

        return {
            "total_questions": len(results),
            "successful_questions": len(successful),
            "metrics_summary": metrics_summary,
            "detail_results": detail_results,
            "completed_at": datetime.now().isoformat(),
        }

    @staticmethod
    def calculate_latency_metrics(
        latency_results: List[Dict[str, Any]],
        threshold: float = 10.0,
    ) -> Dict[str, Any]:
        """
        计算耗时评测整体指标

        Args:
            latency_results: 单条耗时评测结果列表，每个元素包含 duration、passed、threshold 等字段
            threshold: 响应时间阈值（秒）

        Returns:
            包含统计指标的字典
        """
        from app.services.latency_evaluator import LatencyEvaluator

        if not latency_results:
            return {
                "total_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "pass_rate": 0.0,
                "avg_duration": 0.0,
                "min_duration": 0.0,
                "max_duration": 0.0,
                "p50_duration": 0.0,
                "p90_duration": 0.0,
                "p99_duration": 0.0,
                "score": 0.0,
                "threshold": threshold,
                "records": [],
            }

        durations = [r.get("duration", 0) for r in latency_results if r.get("success")]
        input_tokens_list = [r.get("input_tokens") for r in latency_results if r.get("success")]
        output_tokens_list = [r.get("output_tokens") for r in latency_results if r.get("success")]

        evaluator = LatencyEvaluator(threshold=threshold)
        result = evaluator.evaluate(
            durations,
            input_tokens=input_tokens_list,
            output_tokens=output_tokens_list,
        )

        # pass^k 聚合（M2 QE2-3）：同一样本（latency_config.repeat_count>1 时
        # 展开的同 idx 重复行）k 次全过阈值的比例。存在重复行才写（k=1 的
        # 存量聚合结构不变，pass_pow_k 此时与 pass_rate 同值无新信息）。
        sample_passes: Dict[Any, List[bool]] = {}
        for r in latency_results:
            if not r.get("success") or r.get("duration") is None:
                continue
            sample_passes.setdefault(r.get("idx"), []).append(r.get("duration", 0) <= threshold)
        pass_k_block = None
        if any(len(passes) > 1 for passes in sample_passes.values()):
            sample_count = len(sample_passes)
            pass_k_block = {
                "trials": max(len(p) for p in sample_passes.values()),
                "rows": sample_count,
                "average_pass_at_1": round(
                    sum(sum(p) / len(p) for p in sample_passes.values()) / sample_count, 4
                ),
                "average_pass_pow_k": round(
                    sum(1 for p in sample_passes.values() if all(p)) / sample_count, 4
                ),
                "all_pass_rows": sum(1 for p in sample_passes.values() if all(p)),
            }

        latency_metrics = {
            "total_count": result.total_count,
            "passed_count": result.passed_count,
            "failed_count": result.failed_count,
            "pass_rate": result.pass_rate,
            "avg_duration": result.avg_duration,
            "min_duration": result.min_duration,
            "max_duration": result.max_duration,
            "p50_duration": result.p50_duration,
            "p90_duration": result.p90_duration,
            "p99_duration": result.p99_duration,
            "score": result.score,
            "threshold": result.threshold,
            "records": [
                {
                    "index": r.index,
                    "duration": r.duration,
                    "timestamp": r.timestamp,
                    "passed": r.passed,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                }
                for r in result.records
            ],
        }
        if pass_k_block is not None:
            latency_metrics["pass_k"] = pass_k_block
        return latency_metrics


class ResultExporter:
    """结果导出服务"""

    # 导出时排除的内部字段
    _EXPORT_EXCLUDE_KEYS = {
        "idx", "success", "error", "judge_scores", "metrics", "repeat_results",
        "judge_assessment", "evaluation_content", "model_output", "average_score",
        "judge_count", "judge_details", "feedback", "dimension_scores",
        "input_data", "model_raw_response", "question_index", "scores",
        "reasons", "contexts", "contexts_count", "expected_label",
        "predicted_label", "is_correct", "duration", "custom_dimensions",
        "pre_script_results",
    }

    @classmethod
    def _collect_input_columns(cls, results: List[Dict[str, Any]]) -> List[str]:
        """从结果中收集动态输入变量列名"""
        seen = set()
        columns = []
        for r in results:
            for k in r.keys():
                if k not in cls._EXPORT_EXCLUDE_KEYS and k not in seen and not k.startswith("_"):
                    columns.append(k)
                    seen.add(k)
        return columns

    @staticmethod
    def _build_binary_classification_summary(result_data: dict) -> list:
        """构建二分分类评测统计摘要数据"""
        if not result_data:
            return [{"指标": "无数据", "值": "-"}]

        cm = result_data.get("confusion_matrix", {})
        tp = cm.get("tp", 0)
        fp = cm.get("fp", 0)
        tn = cm.get("tn", 0)
        fn = cm.get("fn", 0)
        total = result_data.get("total_count", 0)
        correct = tp + tn

        summary = [
            {"指标": "总样本数", "值": total},
            {"指标": "正确数", "值": correct},
            {"指标": "错误数", "值": total - correct},
            {"指标": "准确率 (Accuracy)", "值": f"{result_data.get('accuracy', 0) * 100:.2f}%"},
            {"指标": "精确率 (Precision)", "值": f"{result_data.get('precision', 0) * 100:.2f}%"},
            {"指标": "灵敏度 (Recall)", "值": f"{result_data.get('sensitivity', 0) * 100:.2f}%"},
            {"指标": "特异度 (Specificity)", "值": f"{result_data.get('specificity', 0) * 100:.2f}%"},
            {"指标": "F1 Score", "值": f"{result_data.get('f1_score', 0) * 100:.2f}%"},
            {"指标": "F2 Score (偏重召回)", "值": f"{result_data.get('f2_score', 0) * 100:.2f}%"},
            {"指标": "F0.5 Score (偏重精确)", "值": f"{result_data.get('f05_score', 0) * 100:.2f}%"},
            {"指标": "MCC", "值": round(result_data.get('mcc', 0), 4)},
            {"指标": "阴性预测值 (NPV)", "值": f"{result_data.get('npv', 0) * 100:.2f}%"},
            {"指标": "错误率", "值": f"{result_data.get('error_rate', 0) * 100:.2f}%"},
            {"指标": "阳性率 (Prevalence)", "值": f"{result_data.get('prevalence', 0) * 100:.2f}%"},
            {"指标": "Youden 指数", "值": round(result_data.get('youden_index', 0), 4)},
            {"指标": "", "值": ""},
            {"指标": "混淆矩阵", "值": "数量"},
            {"指标": "TP (真正例)", "值": tp},
            {"指标": "FP (假正例)", "值": fp},
            {"指标": "TN (真负例)", "值": tn},
            {"指标": "FN (假负例)", "值": fn},
        ]

        return summary

    @staticmethod
    def export_to_excel(results: List[Dict[str, Any]], evaluation_type: str) -> bytes:
        """
        导出为Excel

        Args:
            results: 评测结果列表
            evaluation_type: 评测类型

        Returns:
            Excel文件字节数据
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("需要安装 pandas 库来导出 Excel")

        # W17：类型导出收敛注册表——已迁移类型的 excel 构建由适配器提供，
        # 未迁移/未知类型沿用下方分支与 batch/通用回落，行为不变
        adapter = evaluator_registry.get(evaluation_type)
        if adapter is not None:
            built = adapter.export_excel(results)
            if built is not None:
                return built

        if evaluation_type == "consistency":
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

        elif evaluation_type == "batch":
            # 批量导出格式：支持聚合结果结构，提取 items 进行导出
            data = []
            for r in results:
                # r 是聚合后的 task.result，包含 items 数组
                items = r.get("items", [])
                eval_type = r.get("evaluation_type", "")
                for idx, item in enumerate(items):
                    judge_details = item.get("judge_details", [])
                    if eval_type == "judge" and judge_details:
                        for jd in judge_details:
                            dim_scores_str = json.dumps(jd.get("dimension_scores", {}) or {}, ensure_ascii=False)
                            row = {
                                "题目序号": idx + 1,
                                "评测类型": eval_type,
                                "模型输出": (item.get("model_output") or "")[:100],
                                "平均评分": item.get("average_score", ""),
                                "裁判数量": item.get("judge_count", ""),
                                "裁判序号": jd.get("judge_index", 0) + 1,
                                "裁判评分": jd.get("score", ""),
                                "裁判反馈": (jd.get("feedback") or "")[:100],
                                "裁判各维度评分": dim_scores_str[:100],
                            }
                            data.append(row)
                        continue
                    # 通用 row 构建逻辑
                    row = {
                        "题目序号": idx + 1,
                        "评测类型": eval_type,
                        "模型输出": (item.get("model_output") or "")[:100],
                        "平均评分": item.get("average_score", ""),
                        "裁判数量": item.get("judge_count", ""),
                        "反馈": (item.get("feedback") or "")[:100],
                    }
                    # 根据评测类型添加特定字段
                    if eval_type == "judge":
                        row["评分"] = item.get("average_score", "")
                    elif eval_type == "consistency":
                        row["文本相似度"] = item.get("metrics", {}).get("text_similarity", "")
                        row["格式一致性"] = item.get("metrics", {}).get("format_consistency", "")
                        row["结构一致性"] = item.get("metrics", {}).get("structure_consistency", "")
                    elif eval_type == "success_rate":
                        row["成功率"] = item.get("metrics", {}).get("success_rate", "")
                    data.append(row)
            df = pd.DataFrame(data)

        else:
            # 通用格式
            data = [{"result": json.dumps(r, ensure_ascii=False)} for r in results]
            df = pd.DataFrame(data)

        # 导出为Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='评测结果')

        return output.getvalue()

    @staticmethod
    def export_to_json(results: List[Dict[str, Any]], evaluation_type: str) -> str:
        """
        导出为JSON

        Args:
            results: 评测结果列表
            evaluation_type: 评测类型

        Returns:
            JSON字符串
        """
        return json.dumps({
            "evaluation_type": evaluation_type,
            "export_time": datetime.now().isoformat(),
            "total": len(results),
            "results": results,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def export_to_csv(results: List[Dict[str, Any]], evaluation_type: str) -> str:
        """
        导出为CSV

        Args:
            results: 评测结果列表
            evaluation_type: 评测类型

        Returns:
            CSV字符串
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("需要安装 pandas 库来导出 CSV")

        # W17：类型导出收敛注册表——已迁移类型的 csv 构建由适配器提供，
        # 未迁移/未知类型沿用下方分支与 batch/通用回落，行为不变
        adapter = evaluator_registry.get(evaluation_type)
        if adapter is not None:
            built = adapter.export_csv(results)
            if built is not None:
                return built

        if evaluation_type == "batch":
            # 批量导出格式：支持聚合结果结构，提取 items 进行导出
            data = []
            for r in results:
                items = r.get("items", [])
                eval_type = r.get("evaluation_type", "")
                for idx, item in enumerate(items):
                    judge_details = item.get("judge_details", [])
                    if eval_type == "judge" and judge_details:
                        for jd in judge_details:
                            dim_scores_str = json.dumps(jd.get("dimension_scores", {}) or {}, ensure_ascii=False)
                            row = {
                                "question_index": idx + 1,
                                "evaluation_type": eval_type,
                                "model_output": (item.get("model_output") or "")[:100],
                                "average_score": item.get("average_score", ""),
                                "judge_count": item.get("judge_count", ""),
                                "judge_index": jd.get("judge_index", 0) + 1,
                                "judge_score": jd.get("score", ""),
                                "judge_feedback": (jd.get("feedback") or "")[:100],
                                "judge_dimension_scores": dim_scores_str[:100],
                            }
                            data.append(row)
                        continue
                    row = {
                        "question_index": idx + 1,
                        "evaluation_type": eval_type,
                        "model_output": item.get("model_output", ""),
                        "average_score": item.get("average_score", ""),
                        "judge_count": item.get("judge_count", ""),
                        "feedback": item.get("feedback", ""),
                    }
                    if eval_type == "judge":
                        row["score"] = item.get("average_score", "")
                    elif eval_type == "consistency":
                        row["text_similarity"] = item.get("metrics", {}).get("text_similarity", "")
                        row["format_consistency"] = item.get("metrics", {}).get("format_consistency", "")
                        row["structure_consistency"] = item.get("metrics", {}).get("structure_consistency", "")
                    elif eval_type == "success_rate":
                        row["success_rate"] = item.get("metrics", {}).get("success_rate", "")
                    data.append(row)
            df = pd.DataFrame(data)
        else:
            df = pd.DataFrame(results)

        return df.to_csv(index=False)
