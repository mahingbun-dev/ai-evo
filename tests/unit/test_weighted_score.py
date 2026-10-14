"""加权平均计算核心方法单元测试"""
import json
from unittest.mock import AsyncMock, patch
import pytest
from app.services.judge_evaluation_engine import JudgeEvaluationEngine, WEIGHT_MAP

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestWeightMap:
    """WEIGHT_MAP 常量测试"""

    def test_weight_map_values(self):
        assert WEIGHT_MAP == {"高": 3, "中": 2, "低": 1}


class TestCalculateWeightedScore:
    """calculate_weighted_score 静态方法测试"""

    def test_normal_weighted(self):
        """正常加权: 维度A(权重3, 分数8) + 维度B(权重2, 分数6) → 7.2"""
        metrics = [
            {"name": "维度A", "weight": 3},
            {"name": "维度B", "weight": 2},
        ]
        scores = {"维度A": {"score": 8}, "维度B": {"score": 6}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 7.2

    def test_text_weight(self):
        """文字权重: 高=3, 中=2, 低=1"""
        metrics = [
            {"name": "准确", "weight": "高"},
            {"name": "流畅", "weight": "中"},
            {"name": "创新", "weight": "低"},
        ]
        scores = {
            "准确": {"score": 9},
            "流畅": {"score": 7},
            "创新": {"score": 5},
        }
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        # (9*3 + 7*2 + 5*1) / (3+2+1) = (27+14+5)/6 = 46/6 ≈ 7.67
        assert result == 7.67

    def test_numeric_weight(self):
        """数值权重: 直接使用"""
        metrics = [
            {"name": "A", "weight": 4},
            {"name": "B", "weight": 1},
        ]
        scores = {"A": {"score": 10}, "B": {"score": 0}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        # (10*4 + 0*1) / 5 = 8.0
        assert result == 8.0

    def test_mixed_weight(self):
        """混合权重: 部分文字部分数值"""
        metrics = [
            {"name": "A", "weight": "高"},
            {"name": "B", "weight": 1},
        ]
        scores = {"A": {"score": 6}, "B": {"score": 10}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        # (6*3 + 10*1) / (3+1) = 28/4 = 7.0
        assert result == 7.0

    def test_clamp_upper(self):
        """clamp 上限: 计算结果超过 max_score → 返回 max_score"""
        metrics = [{"name": "A", "weight": 1}]
        scores = {"A": {"score": 10}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 5)
        assert result == 5

    def test_clamp_lower(self):
        """clamp 下限: 计算结果为负 → 返回 0"""
        metrics = [{"name": "A", "weight": 1}]
        scores = {"A": {"score": -5}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 0.0

    def test_empty_dimension_scores(self):
        """空维度: dimension_scores 为空 → 返回 0.0"""
        metrics = [{"name": "A", "weight": 1}]
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, {}, 10)
        assert result == 0.0

    def test_empty_metrics(self):
        """空 metrics: evaluation_metrics 为空 → 返回 0.0"""
        scores = {"A": {"score": 8}}
        result = JudgeEvaluationEngine.calculate_weighted_score([], scores, 10)
        assert result == 0.0

    def test_string_score(self):
        """维度评分格式: score 为字符串 '8' → 正确转换为 float"""
        metrics = [{"name": "A", "weight": 1}]
        scores = {"A": {"score": "8"}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 8.0

    def test_dimension_score_key(self):
        """dimension_score key: 维度数据用 'dimension_score' 而非 'score' → 正确识别"""
        metrics = [{"name": "A", "weight": 1}]
        scores = {"A": {"dimension_score": 7.5}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 7.5

    def test_unknown_text_weight_defaults_to_1(self):
        """未知文字权重默认为 1"""
        metrics = [{"name": "A", "weight": "特殊"}]
        scores = {"A": {"score": 6}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 6.0

    def test_zero_total_weight(self):
        """所有权重为 0 → 返回 0.0"""
        metrics = [
            {"name": "A", "weight": 0},
            {"name": "B", "weight": 0},
        ]
        scores = {"A": {"score": 5}, "B": {"score": 5}}
        result = JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10)
        assert result == 0.0


class TestEngineIntegration:
    """评测引擎集成加权计算测试"""

    def _make_engine(self, metrics=None):
        """创建一个带评测维度配置的引擎实例"""
        model_config = {"model_type": "test"}
        judge_config = {"model_type": "test"}
        engine = JudgeEvaluationEngine(
            model_config=model_config,
            judge_config=judge_config,
            evaluation_metrics=metrics or [],
        )
        return engine

    @pytest.mark.asyncio
    async def test_evaluate_single_overrides_llm_score(self):
        """evaluate_single 覆盖: mock LLM 返回综合评分=7，加权计算=7.2 → 最终=7.2"""
        metrics = [
            {"name": "维度A", "weight": 3},
            {"name": "维度B", "weight": 2},
        ]
        engine = self._make_engine(metrics)
        llm_response = json.dumps({
            "综合评分": 7,
            "综合评分原因": "质量一般",
            "各维度评分": {
                "维度A": {"score": 8, "analysis": "不错"},
                "维度B": {"score": 6, "analysis": "还行"},
            }
        })
        with patch.object(engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await engine.evaluate_single("q", "ref", "ans")
        assert result.overall_score == 7.2

    @pytest.mark.asyncio
    async def test_evaluate_with_targets_overrides_llm_score(self):
        """_evaluate_with_targets 覆盖: mock 返回综合评分=7，加权计算=7.2 → 最终=7.2"""
        metrics = [
            {"name": "维度A", "weight": 3},
            {"name": "维度B", "weight": 2},
        ]
        targets = [{"id": "t1", "name": "测试目标", "display_name": "测试目标", "source": "model_output", "source_config": {}}]
        engine = self._make_engine(metrics)
        engine.evaluation_targets = targets
        llm_response = json.dumps({
            "综合评分": 7,
            "综合评分原因": "一般",
            "各维度评分": {
                "维度A": {"score": 8},
                "维度B": {"score": 6},
            }
        })
        with patch.object(engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await engine.evaluate_single("q", "ref", "ans")
        assert result.overall_score == 7.2

    @pytest.mark.asyncio
    async def test_no_dimension_scores_falls_back_to_llm(self):
        """无维度评分回退: dimension_scores 为空 → 使用 LLM 返回的综合评分"""
        engine = self._make_engine([])  # 无维度配置
        llm_response = json.dumps({
            "综合评分": 7.5,
            "综合评分原因": "质量不错",
        })
        with patch.object(engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await engine.evaluate_single("q", "ref", "ans")
        assert result.overall_score == 7.5
