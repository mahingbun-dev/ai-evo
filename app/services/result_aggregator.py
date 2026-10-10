"""
结果聚合与导出服务
"""
import json
import io
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.services.judge_evaluation_engine import JudgeEvaluationResult


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

        # 过滤成功的结果
        successful_results = [r for r in results if r.get("success", False)]
        failed_results = [r for r in results if not r.get("success", False)]

        # 计算统计信息
        total = len(results)
        success_count = len(successful_results)
        failed_count = len(failed_results)

        # 计算平均分
        scores = [r.get("average_score", 0) for r in successful_results if r.get("average_score") is not None]
        average_score = sum(scores) / len(scores) if scores else 0

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
            "items": self._format_items(successful_results),
            "failed_items": self._format_failed_items(failed_results),
            "completed_at": datetime.now().isoformat(),
        }

        return aggregated

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
            "pre_script_results",
        }

        for idx, result in enumerate(results):
            judge_scores = result.get("judge_scores", [])
            item = {
                "idx": result.get("idx", idx),
                "success": True,
                "model_output": result.get("model_output", ""),
                "model_raw_response": result.get("model_raw_response", ""),
                "input_data": result.get("input_data", ""),
                "average_score": result.get("average_score", 0),
                "judge_count": len(judge_scores),
                "judge_scores": judge_scores,
                "judge_details": [
                    {
                        "judge_index": i,
                        "score": js.get("score"),
                        "feedback": js.get("feedback"),
                        "dimension_scores": js.get("dimension_scores"),
                        "evaluation_target_name": js.get("evaluation_target_name") or f"裁判{i+1}",
                        "langfuse_trace_id": js.get("langfuse_trace_id"),
                        "langfuse_trace_url": js.get("langfuse_trace_url"),
                    }
                    for i, js in enumerate(judge_scores)
                ] if judge_scores else [],
                "feedback": result.get("feedback", ""),
                "dimension_scores": result.get("dimension_scores", {}),
            }
            # 前处理脚本结果全量入库（设计文档 §三 结果展示通道 / §六 结果结构），
            # judge 与 agent 两种评测类型均生效
            if "pre_script_results" in result:
                item["pre_script_results"] = result["pre_script_results"]
            # 平铺输入变量（排除结构化字段、内部字段和空值字段）
            for k, v in result.items():
                if k not in structured_keys and not k.startswith("_") and v is not None:
                    item[k] = v
            items.append(item)

        return items

    def _format_failed_items(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """格式化失败结果"""
        items = []

        for idx, result in enumerate(results):
            item = {
                "idx": result.get("idx", idx),
                "success": False,
                "error": result.get("error", "Unknown error"),
            }
            # on_fail=abort 失败的条目保留脚本结果，便于定位失败脚本
            if result.get("pre_script_results"):
                item["pre_script_results"] = result["pre_script_results"]
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

        return {
            "total_questions": len(results),
            "average_text_similarity": round(sum(text_similarities) / len(text_similarities), 4) if text_similarities else 0,
            "average_format_consistency": round(sum(format_consistencies) / len(format_consistencies), 4) if format_consistencies else 0,
            "average_structure_consistency": round(sum(structure_consistencies) / len(structure_consistencies), 4) if structure_consistencies else 0,
            "items": detailed_items,
            "custom_dimension_summary": aggregated_dims,  # 保持向后兼容
            "dimension_scores": aggregated_dims,  # 统一的维度评分汇总
        }

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

        return {
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

        if evaluation_type == "judge":
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

        elif evaluation_type == "consistency":
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

        elif evaluation_type == "success_rate":
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

        elif evaluation_type == "rag":
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

        elif evaluation_type == "latency":
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

        elif evaluation_type == "binary_classification":
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
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_summary.to_excel(writer, index=False, sheet_name='统计摘要')
                df_detail.to_excel(writer, index=False, sheet_name='逐条明细')

            return output.getvalue()

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

        if evaluation_type == "judge":
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

        elif evaluation_type == "batch":
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
        elif evaluation_type == "latency":
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
        elif evaluation_type == "binary_classification":
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
        else:
            df = pd.DataFrame(results)

        return df.to_csv(index=False)
