"""
DeepEval 结果转换器

将 DeepEval 的评测结果转换为 AI-EVO 的结果格式。
支持：
- 转换单个指标评测结果
- 将多个指标结果聚合为评测结果格式
- 计算加权分数
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# 权重映射：文字权重 -> 数值权重
WEIGHT_MAP: Dict[str, int] = {"高": 3, "中": 2, "低": 1}


@dataclass
class TransformedMetricResult:
    """转换后的单个指标结果"""
    metric_key: str
    metric_name: str
    # W16 异常语义修正：适配失败（指标不存在/网关异常等）时 score 为 None
    # （无分数），不再伪装 0.0；正常评测路径仍为 float
    score: Optional[float] = 0.0
    reason: Optional[str] = None
    success: bool = False
    score_breakdown: Optional[Dict[str, Any]] = None
    verdicts: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None
    # W16：适配失败标记——True 表示该指标本次未产出有效评分（指标不存在、
    # 平台网关异常、配置解析失败等），下游不得把此类结果当 0 分参与统计
    metric_unavailable: bool = False


@dataclass
class TransformedEvaluationResult:
    """转换后的评测结果"""
    overall_score: float = 0.0
    dimension_scores: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_metrics: int = 0
    passed_metrics: int = 0
    failed_metrics: int = 0


class DeepEvalResultTransformer:
    """DeepEval 结果转换器，将 DeepEval 评测结果转换为 AI-EVO 结果格式"""

    @staticmethod
    def transform_metric_result(
        metric_result: Any,
        metric_key: str,
        metric_name: str,
    ) -> TransformedMetricResult:
        """
        转换单个 DeepEval 指标的评测结果。

        Args:
            metric_result: DeepEval 指标结果对象，需包含 score, reason, success 等属性
            metric_key: 指标唯一标识符
            metric_name: 指标显示名称

        Returns:
            TransformedMetricResult: 转换后的指标结果
        """
        score = getattr(metric_result, "score", None)
        reason = getattr(metric_result, "reason", None)
        success = getattr(metric_result, "success", None)
        score_breakdown = getattr(metric_result, "score_breakdown", None)
        error = getattr(metric_result, "error", None)
        # W16 复核修复：重建对象时必须保留 metric_unavailable 标记——
        # transform_to_evaluation_result 对输入再走一遍本方法重建，
        # 丢失该标记会让 dimension_scores 恒 False，下游无法区分
        # 「真 0 分」与「指标不可用」
        metric_unavailable = getattr(metric_result, "metric_unavailable", False)

        # 处理 score 为 None 的情况，默认 0.0
        if score is None:
            score = 0.0

        # 处理 success 为 None 的情况：有错误或 score 为 0 时视为失败
        if success is None:
            success = score > 0.0 and error is None

        # 转换 verdicts 为字典列表
        verdicts = None
        raw_verdicts = getattr(metric_result, "verdicts", None)
        if raw_verdicts is not None:
            verdicts = []
            for v in raw_verdicts:
                verdicts.append({
                    "verdict": getattr(v, "verdict", ""),
                    "reason": getattr(v, "reason", ""),
                })

        return TransformedMetricResult(
            metric_key=metric_key,
            metric_name=metric_name,
            score=score,
            reason=reason,
            success=success,
            score_breakdown=score_breakdown,
            verdicts=verdicts,
            error=error,
            metric_unavailable=metric_unavailable,
        )

    @staticmethod
    def transform_to_evaluation_result(
        metric_results: List[Any],
        metric_configs: List[Dict[str, Any]],
        max_score: float = 1.0,
    ) -> TransformedEvaluationResult:
        """
        将多个 DeepEval 指标结果转换为评测结果格式。

        计算 overall_score（加权平均），生成 dimension_scores 字典。

        Args:
            metric_results: DeepEval 指标结果列表
            metric_configs: 指标配置列表，每个包含 key, name, weight
            max_score: 最大分数，用于映射和限制分数范围

        Returns:
            TransformedEvaluationResult: 转换后的评测结果
        """
        if not metric_results or not metric_configs:
            return TransformedEvaluationResult(
                overall_score=0.0,
                dimension_scores={},
                total_metrics=0,
                passed_metrics=0,
                failed_metrics=0,
            )

        dimension_scores: Dict[str, Dict[str, Any]] = {}
        scores_for_weighted: Dict[str, float] = {}
        weights_for_weighted: Dict[str, Any] = {}
        passed_count = 0
        failed_count = 0

        for metric_result, config in zip(metric_results, metric_configs):
            key = config["key"]
            name = config["name"]
            weight = config.get("weight", 1)

            transformed = DeepEvalResultTransformer.transform_metric_result(
                metric_result=metric_result,
                metric_key=key,
                metric_name=name,
            )

            # 将 score 映射到 max_score 分制
            # W16：metric_unavailable 结果的 score 为 None（无分数），
            # 聚合口径维持既有约定按 0 参与，不因 None 崩溃
            mapped_score = (transformed.score or 0.0) * max_score

            dimension_scores[key] = {
                "score": mapped_score,
                "name": name,
                "success": transformed.success,
                "reason": transformed.reason,
                "metric_unavailable": transformed.metric_unavailable,
            }

            scores_for_weighted[key] = mapped_score
            weights_for_weighted[key] = weight

            if transformed.success:
                passed_count += 1
            else:
                failed_count += 1

        # 计算加权平均分并映射到 max_score 分制
        weighted_score = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores_for_weighted,
            weights=weights_for_weighted,
            max_score=max_score,
        )

        return TransformedEvaluationResult(
            overall_score=weighted_score,
            dimension_scores=dimension_scores,
            total_metrics=len(metric_results),
            passed_metrics=passed_count,
            failed_metrics=failed_count,
        )

    @staticmethod
    def calculate_weighted_score(
        scores: Dict[str, float],
        weights: Dict[str, Any],
        max_score: float = 1.0,
    ) -> float:
        """
        计算加权分数。

        支持文字权重映射：高=3, 中=2, 低=1。
        结果限制在 [0, max_score] 范围内。

        Args:
            scores: 分数字典，key 为指标名，value 为分数（0-1 范围）
            weights: 权重字典，key 为指标名，value 为权重（文字或数值）
            max_score: 最大分数限制

        Returns:
            float: 加权平均分数，限制在 [0, max_score] 范围
        """
        if not scores or not weights:
            return 0.0

        total_weighted = 0.0
        total_weight = 0.0

        for key, score in scores.items():
            weight = weights.get(key, 1)

            # 处理文字权重
            if isinstance(weight, str):
                weight = WEIGHT_MAP.get(weight, 1)

            total_weighted += score * weight
            total_weight += weight

        if total_weight == 0:
            return 0.0

        result = total_weighted / total_weight

        # 限制在 [0, max_score] 范围内
        result = max(0.0, min(result, max_score))

        return result
