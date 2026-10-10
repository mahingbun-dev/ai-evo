"""一致性评测加权计算单元测试"""
import json
from unittest.mock import AsyncMock, patch
import pytest
from app.services.consistency_evaluator import ConsistencyEvaluator


class TestConsistencyWeightedScore:
    """验证一致性评测路径中加权计算正确"""

    def _make_evaluator(self, metrics=None, max_score=10):
        model_config = {"model_type": "test", "api_key": "test", "endpoint": "http://test"}
        judge_config = {"model_type": "test", "api_key": "test", "endpoint": "http://test"}
        return ConsistencyEvaluator(
            model_config=model_config,
            judge_config=judge_config,
            evaluation_metrics=metrics or [],
            max_score=max_score,
            judge_variable_config={"user_input": {"source": "dataset_field"}},
        )

    @pytest.mark.asyncio
    async def test_judge_evaluate_overrides_llm_score(self):
        """_judge_evaluate 覆盖: mock 裁判返回综合评分=7，维度评分加权=7.2 → 最终=7.2"""
        metrics = [
            {"name": "维度A", "weight": 3},
            {"name": "维度B", "weight": 2},
        ]
        evaluator = self._make_evaluator(metrics)
        llm_response = json.dumps({
            "综合评分": 7,
            "综合评分原因": "一般",
            "各维度评分": {
                "维度A": {"score": 8},
                "维度B": {"score": 6},
            }
        })
        with patch.object(evaluator.engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await evaluator.evaluate_single("test question", repeat_count=1, use_judge=True)
        assert result.judge_assessment["综合评分"] == 7.2

    @pytest.mark.asyncio
    async def test_no_dimension_scores_falls_back(self):
        """无维度评分回退: dimension_scores 为空 → 使用 LLM 返回值"""
        evaluator = self._make_evaluator([])  # 无维度配置
        llm_response = json.dumps({
            "综合评分": 7.5,
            "综合评分原因": "不错",
        })
        with patch.object(evaluator.engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await evaluator.evaluate_single("test question", repeat_count=1, use_judge=True)
        assert result.judge_assessment["综合评分"] == 7.5

    @pytest.mark.asyncio
    async def test_text_weight_mapping(self):
        """文字权重映射: weight='高' → 映射为 3 参与计算"""
        metrics = [
            {"name": "准确性", "weight": "高"},
            {"name": "流畅性", "weight": "低"},
        ]
        evaluator = self._make_evaluator(metrics)
        llm_response = json.dumps({
            "综合评分": 5,
            "各维度评分": {
                "准确性": {"score": 9},
                "流畅性": {"score": 3},
            }
        })
        with patch.object(evaluator.engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await evaluator.evaluate_single("test question", repeat_count=1, use_judge=True)
        # (9*3 + 3*1) / (3+1) = 30/4 = 7.5
        assert result.judge_assessment["综合评分"] == 7.5

    @pytest.mark.asyncio
    async def test_max_score_clamp(self):
        """max_score 生效: max_score=5 时，加权结果超过 5 → clamp 返回 5"""
        metrics = [{"name": "准确性", "weight": 1}]
        evaluator = self._make_evaluator(metrics, max_score=5)
        llm_response = json.dumps({
            "综合评分": 8,
            "各维度评分": {
                "准确性": {"score": 8},
            }
        })
        with patch.object(evaluator.engine, '_call_model', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"success": True, "content": llm_response}
            result = await evaluator.evaluate_single("test question", repeat_count=1, use_judge=True)
        assert result.judge_assessment["综合评分"] == 5

    def test_max_score_not_hardcoded(self):
        """验证 max_score 不再硬编码为 10"""
        evaluator = self._make_evaluator(max_score=5)
        config = evaluator._get_prompt_builder_config()
        assert config["max_score"] == 5

    def test_no_calculate_weighted_score_method_on_evaluator(self):
        """验证 ConsistencyEvaluator 不定义 calculate_weighted_score 方法"""
        assert not hasattr(ConsistencyEvaluator, 'calculate_weighted_score')
