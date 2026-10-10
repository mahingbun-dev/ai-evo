"""
DeepEval 结果转换器单元测试

测试 DeepEval 评测结果到 AI-EVO 结果格式的转换逻辑。
"""
import pytest
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from app.services.deepeval_result_transformer import (
    DeepEvalResultTransformer,
    TransformedMetricResult,
    TransformedEvaluationResult,
)


# ============================================================================
# Mock 数据：模拟 DeepEval 指标结果
# ============================================================================
@dataclass
class MockMetricVerdict:
    """模拟 DeepEval 的 verdict 对象"""
    verdict: str
    reason: str


@dataclass
class MockMetricResult:
    """模拟 DeepEval 的指标结果"""
    score: Optional[float] = None
    reason: Optional[str] = None
    success: Optional[bool] = None
    score_breakdown: Optional[Dict] = None
    verdicts: Optional[List[MockMetricVerdict]] = None
    error: Optional[str] = None


# ============================================================================
# 测试 1: transform_metric_result - 转换单个指标结果
# ============================================================================
class TestTransformMetricResult:
    """测试转换单个指标的评测结果"""

    def test_transform_metric_with_score(self):
        """测试转换包含分数的指标结果"""
        metric = MockMetricResult(
            score=0.85,
            reason="答案质量良好",
            success=True,
            score_breakdown={"relevancy": 0.9, "coherence": 0.8},
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="answer_relevancy",
            metric_name="答案相关性",
        )
        assert result.score == 0.85
        assert result.reason == "答案质量良好"
        assert result.success is True
        assert result.metric_key == "answer_relevancy"
        assert result.metric_name == "答案相关性"
        assert result.score_breakdown == {"relevancy": 0.9, "coherence": 0.8}

    def test_transform_metric_score_none(self):
        """测试转换分数为 None 的指标结果（默认 0.0）"""
        metric = MockMetricResult(
            score=None,
            reason=None,
            success=None,
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="faithfulness",
            metric_name="忠实度",
        )
        assert result.score == 0.0
        assert result.reason is None
        assert result.success is False

    def test_transform_metric_with_verdicts(self):
        """测试转换包含 verdicts 的指标结果"""
        verdicts = [
            MockMetricVerdict(verdict="yes", reason="声明有依据"),
            MockMetricVerdict(verdict="no", reason="声明无依据"),
            MockMetricVerdict(verdict="idk", reason="无法判断"),
        ]
        metric = MockMetricResult(
            score=0.67,
            reason="部分声明有依据",
            success=True,
            verdicts=verdicts,
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="faithfulness",
            metric_name="忠实度",
        )
        assert result.score == 0.67
        assert len(result.verdicts) == 3
        assert result.verdicts[0]["verdict"] == "yes"
        assert result.verdicts[0]["reason"] == "声明有依据"

    def test_transform_metric_with_error(self):
        """测试转换包含错误信息的指标结果"""
        metric = MockMetricResult(
            score=None,
            success=None,
            error="API 调用超时",
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="toxicity",
            metric_name="毒性检测",
        )
        assert result.score == 0.0
        assert result.success is False
        assert result.error == "API 调用超时"

    def test_transform_metric_score_zero(self):
        """测试转换分数为 0 的指标结果"""
        metric = MockMetricResult(
            score=0.0,
            reason="完全不相关",
            success=False,
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="answer_relevancy",
            metric_name="答案相关性",
        )
        assert result.score == 0.0
        assert result.success is False

    def test_transform_metric_score_one(self):
        """测试转换分数为 1.0 的指标结果"""
        metric = MockMetricResult(
            score=1.0,
            reason="完美答案",
            success=True,
        )
        result = DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key="answer_relevancy",
            metric_name="答案相关性",
        )
        assert result.score == 1.0
        assert result.success is True


# ============================================================================
# 测试 2: transform_to_evaluation_result - 转换为评测结果格式
# ============================================================================
class TestTransformToEvaluationResult:
    """测试将多个指标结果转换为评测结果格式"""

    def test_transform_multiple_metrics(self):
        """测试转换多个指标结果为评测结果格式"""
        metric_results = [
            MockMetricResult(score=0.9, reason="优秀", success=True),
            MockMetricResult(score=0.7, reason="良好", success=True),
            MockMetricResult(score=0.5, reason="一般", success=False),
        ]
        metric_configs = [
            {"key": "answer_relevancy", "name": "答案相关性", "weight": "高"},
            {"key": "faithfulness", "name": "忠实度", "weight": "中"},
            {"key": "toxicity", "name": "毒性检测", "weight": "低"},
        ]
        result = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=metric_results,
            metric_configs=metric_configs,
            max_score=1.0,
        )
        assert isinstance(result, TransformedEvaluationResult)
        assert result.total_metrics == 3
        assert "answer_relevancy" in result.dimension_scores
        assert "faithfulness" in result.dimension_scores
        assert "toxicity" in result.dimension_scores

    def test_transform_calculate_overall_score(self):
        """测试计算 overall_score（加权平均）"""
        metric_results = [
            MockMetricResult(score=0.9, success=True),   # 权重 3 (高)
            MockMetricResult(score=0.6, success=True),   # 权重 2 (中)
            MockMetricResult(score=0.3, success=False),  # 权重 1 (低)
        ]
        metric_configs = [
            {"key": "a", "name": "A", "weight": "高"},   # 权重 3
            {"key": "b", "name": "B", "weight": "中"},   # 权重 2
            {"key": "c", "name": "C", "weight": "低"},   # 权重 1
        ]
        result = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=metric_results,
            metric_configs=metric_configs,
            max_score=1.0,
        )
        # (0.9*3 + 0.6*2 + 0.3*1) / (3+2+1) = (2.7+1.2+0.3)/6 = 4.2/6 = 0.7
        assert result.overall_score == pytest.approx(0.7)

    def test_transform_empty_metrics(self):
        """测试转换空指标列表"""
        result = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=[],
            metric_configs=[],
            max_score=1.0,
        )
        assert result.overall_score == 0.0
        assert result.total_metrics == 0
        assert result.dimension_scores == {}

    def test_transform_with_max_score_10(self):
        """测试 max_score=10 的场景（映射到 0-10 分制）"""
        metric_results = [
            MockMetricResult(score=0.8, success=True),   # 权重 3
            MockMetricResult(score=0.6, success=True),   # 权重 2
        ]
        metric_configs = [
            {"key": "a", "name": "A", "weight": "高"},   # 权重 3
            {"key": "b", "name": "B", "weight": "中"},   # 权重 2
        ]
        result = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=metric_results,
            metric_configs=metric_configs,
            max_score=10,
        )
        # 映射到 10 分制: 0.8*10=8, 0.6*10=6
        # (8*3 + 6*2) / (3+2) = (24+12)/5 = 36/5 = 7.2
        assert result.overall_score == pytest.approx(7.2)

    def test_transform_dimension_scores_structure(self):
        """测试 dimension_scores 的数据结构"""
        metric_results = [
            MockMetricResult(score=0.8, reason="好", success=True),
        ]
        metric_configs = [
            {"key": "answer_relevancy", "name": "答案相关性", "weight": "高"},
        ]
        result = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=metric_results,
            metric_configs=metric_configs,
            max_score=1.0,
        )
        dim = result.dimension_scores["answer_relevancy"]
        assert dim["score"] == 0.8
        assert dim["name"] == "答案相关性"
        assert dim["success"] is True


# ============================================================================
# 测试 3: calculate_weighted_score - 计算加权分数
# ============================================================================
class TestCalculateWeightedScore:
    """测试计算加权分数"""

    def test_text_weight_high(self):
        """测试文字权重 '高' = 3"""
        scores = {"A": 0.9, "B": 0.6}
        weights = {"A": "高", "B": "低"}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        # (0.9*3 + 0.6*1) / (3+1) = (2.7+0.6)/4 = 3.3/4 = 0.825
        assert result == pytest.approx(0.825)

    def test_text_weight_medium(self):
        """测试文字权重 '中' = 2"""
        scores = {"A": 0.8}
        weights = {"A": "中"}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == pytest.approx(0.8)

    def test_text_weight_low(self):
        """测试文字权重 '低' = 1"""
        scores = {"A": 0.5}
        weights = {"A": "低"}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == pytest.approx(0.5)

    def test_numeric_weight(self):
        """测试数值权重"""
        scores = {"A": 0.9, "B": 0.5}
        weights = {"A": 4, "B": 1}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        # (0.9*4 + 0.5*1) / (4+1) = (3.6+0.5)/5 = 4.1/5 = 0.82
        assert result == pytest.approx(0.82)

    def test_mixed_weights(self):
        """测试混合权重（文字 + 数值）"""
        scores = {"A": 0.8, "B": 0.6}
        weights = {"A": "高", "B": 1}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        # (0.8*3 + 0.6*1) / (3+1) = (2.4+0.6)/4 = 3.0/4 = 0.75
        assert result == pytest.approx(0.75)

    def test_clamp_upper_bound(self):
        """测试结果超过 max_score 时限制为 max_score"""
        scores = {"A": 1.5}
        weights = {"A": 1}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == 1.0

    def test_clamp_lower_bound(self):
        """测试结果为负数时限制为 0"""
        scores = {"A": -0.5}
        weights = {"A": 1}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == 0.0

    def test_max_score_10(self):
        """测试 max_score=10 的场景"""
        scores = {"A": 8.0, "B": 6.0}
        weights = {"A": "高", "B": "中"}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=10
        )
        # (8*3 + 6*2) / (3+2) = (24+12)/5 = 36/5 = 7.2
        assert result == pytest.approx(7.2)

    def test_empty_scores(self):
        """测试空分数字典"""
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores={}, weights={}, max_score=1.0
        )
        assert result == 0.0

    def test_unknown_text_weight_defaults_to_1(self):
        """测试未知文字权重默认为 1"""
        scores = {"A": 0.8}
        weights = {"A": "特殊"}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == pytest.approx(0.8)

    def test_zero_total_weight(self):
        """测试所有权重为 0 时返回 0.0"""
        scores = {"A": 0.8, "B": 0.6}
        weights = {"A": 0, "B": 0}
        result = DeepEvalResultTransformer.calculate_weighted_score(
            scores=scores, weights=weights, max_score=1.0
        )
        assert result == 0.0
