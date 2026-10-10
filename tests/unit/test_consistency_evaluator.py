"""
ConsistencyEvaluator 单元测试
测试 ConsistencyEvaluator 的自定义维度支持
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from app.services.consistency_evaluator import ConsistencyEvaluator


class TestConsistencyEvaluator:
    """ConsistencyEvaluator 测试类"""

    @pytest.fixture
    def mock_model_config(self):
        """模拟模型配置"""
        return {
            "model_name": "test-model",
            "api_key": "test-key",
            "api_url": "https://test.com"
        }

    @pytest.fixture
    def evaluator_without_metrics(self, mock_model_config):
        """无自定义维度的评测器"""
        return ConsistencyEvaluator(
            model_config=mock_model_config,
            evaluation_metrics=[]
        )

    @pytest.fixture
    def evaluator_with_metrics(self, mock_model_config):
        """有自定义维度的评测器"""
        custom_metrics = [
            {
                "name": "逻辑一致性",
                "weight": "高",
                "max_score": 10,
                "description": "评估逻辑是否一致",
                "score_criteria": {
                    "优秀": {"min": 9, "max": 10, "desc": "逻辑完全一致"},
                    "良好": {"min": 6, "max": 8, "desc": "逻辑基本一致"},
                    "较差": {"min": 0, "max": 5, "desc": "逻辑不一致"}
                }
            },
            {
                "name": "表达一致性",
                "weight": "中",
                "max_score": 10,
                "description": "评估表达方式是否一致",
                "score_criteria": {
                    "优秀": {"min": 9, "max": 10, "desc": "表达完全一致"},
                    "良好": {"min": 6, "max": 8, "desc": "表达基本一致"},
                    "较差": {"min": 0, "max": 5, "desc": "表达不一致"}
                }
            }
        ]
        return ConsistencyEvaluator(
            model_config=mock_model_config,
            evaluation_metrics=custom_metrics
        )

    def test_evaluator_init_without_metrics(self, evaluator_without_metrics):
        """测试无自定义维度时初始化"""
        assert evaluator_without_metrics.evaluation_metrics == []

    def test_evaluator_init_with_metrics(self, evaluator_with_metrics):
        """测试有自定义维度时初始化"""
        assert len(evaluator_with_metrics.evaluation_metrics) == 2
        assert evaluator_with_metrics.evaluation_metrics[0]["name"] == "逻辑一致性"
        assert evaluator_with_metrics.evaluation_metrics[1]["name"] == "表达一致性"

    def test_build_legacy_judge_prompt(self, evaluator_without_metrics):
        """测试向后兼容的prompt构建"""
        results = ["回复1内容", "回复2内容", "回复3内容"]
        prompt = evaluator_without_metrics._build_legacy_judge_prompt(results)

        assert "回复1内容" in prompt
        assert "回复2内容" in prompt
        assert "回复3内容" in prompt
        assert "内容一致性" in prompt
        assert "格式一致性" in prompt
        assert "结构一致性" in prompt

    def test_build_judge_prompt_with_custom_metrics(self, evaluator_with_metrics):
        """测试自定义维度的prompt构建"""
        results = ["回复1内容", "回复2内容"]
        prompt = evaluator_with_metrics._build_judge_prompt(results)

        assert "回复1内容" in prompt
        assert "回复2内容" in prompt
        assert "逻辑一致性" in prompt
        assert "表达一致性" in prompt
        assert "评分标准如下" in prompt

    def test_build_judge_prompt_without_metrics_uses_legacy(self, evaluator_without_metrics):
        """测试无自定义维度时使用向后兼容prompt"""
        results = ["回复1", "回复2"]
        prompt = evaluator_without_metrics._build_judge_prompt(results)

        assert "内容一致性" in prompt
        assert "格式一致性" in prompt
        assert "结构一致性" in prompt

    def test_build_judge_prompt_multiple_custom_metrics(self, mock_model_config):
        """测试多个自定义维度的prompt构建"""
        custom_metrics = [
            {"name": "维度1", "weight": "高", "max_score": 10, "description": "描述1"},
            {"name": "维度2", "weight": "中", "max_score": 10, "description": "描述2"},
            {"name": "维度3", "weight": "低", "max_score": 10, "description": "描述3"}
        ]
        evaluator = ConsistencyEvaluator(
            model_config=mock_model_config,
            evaluation_metrics=custom_metrics
        )
        results = ["回复1", "回复2"]
        prompt = evaluator._build_judge_prompt(results)

        assert "维度1" in prompt
        assert "维度2" in prompt
        assert "维度3" in prompt

    def test_evaluation_metrics_parameter_exists(self, mock_model_config):
        """验证evaluation_metrics参数确实存在"""
        evaluator = ConsistencyEvaluator(
            model_config=mock_model_config,
            evaluation_metrics=[{"name": "测试维度", "weight": "高"}]
        )
        assert hasattr(evaluator, 'evaluation_metrics')
        assert len(evaluator.evaluation_metrics) == 1


class TestJudgeEvaluateScoreExtraction:
    """测试 _judge_evaluate 中评分提取逻辑对不同 JSON 格式的兼容性"""

    @staticmethod
    def _extract_scores(scores: dict) -> dict:
        """模拟 consistency_evaluator.py 中 _judge_evaluate 的评分提取逻辑"""
        overall_score = scores.get(
            "综合评分",
            scores.get("overall_score", scores.get("score", 0))
        )
        reasoning = scores.get(
            "综合评分原因",
            scores.get("reasoning", scores.get("feedback", ""))
        )
        dimension_scores = scores.get(
            "各维度评分",
            scores.get("dimension_scores", {})
        )
        if not dimension_scores and isinstance(scores, dict):
            for k, v in scores.items():
                if isinstance(v, dict) and "score" in v:
                    dimension_scores[k] = v
        return {
            "综合评分": overall_score,
            "综合评分原因": reasoning,
            "各维度评分": dimension_scores if isinstance(dimension_scores, dict) else {},
        }

    def test_prompt_builder_format_score_key(self):
        """测试 PromptBuilder 格式: {"score": 8.5, "dimension_scores": {...}, "feedback": "..."}"""
        scores = {
            "score": 10,
            "dimension_scores": {"测试维度": 10},
            "feedback": "直接满分"
        }
        result = self._extract_scores(scores)
        assert result["综合评分"] == 10
        assert result["各维度评分"] == {"测试维度": 10}
        assert result["综合评分原因"] == "直接满分"

    def test_legacy_chinese_format(self):
        """测试旧格式: {"综合评分": 10, "综合评分原因": "...", "各维度评分": {...}}"""
        scores = {
            "综合评分": 8,
            "综合评分原因": "表现良好",
            "各维度评分": {"测试维度": {"score": 8, "analysis": "还行"}}
        }
        result = self._extract_scores(scores)
        assert result["综合评分"] == 8
        assert result["各维度评分"] == {"测试维度": {"score": 8, "analysis": "还行"}}

    def test_default_template_flat_dimensions(self):
        """测试 DEFAULT_EVALUATION_PROMPT_TEMPLATE 格式: 顶层 key 是维度名"""
        scores = {
            "综合评分": 9,
            "综合评分原因": "优秀",
            "测试维度": {"score": 9, "analysis": "非常好"}
        }
        result = self._extract_scores(scores)
        assert result["综合评分"] == 9
        assert "测试维度" in result["各维度评分"]
        assert result["各维度评分"]["测试维度"]["score"] == 9

    def test_overall_score_key(self):
        """测试 overall_score 格式"""
        scores = {
            "overall_score": 7.5,
            "reasoning": "中等",
            "dimension_scores": {"逻辑一致性": 8, "表达一致性": 7}
        }
        result = self._extract_scores(scores)
        assert result["综合评分"] == 7.5
        assert result["综合评分原因"] == "中等"
        assert result["各维度评分"] == {"逻辑一致性": 8, "表达一致性": 7}

    def test_missing_all_keys_defaults_to_zero(self):
        """测试所有 key 都缺失时默认为 0"""
        scores = {"some_random_key": "value"}
        result = self._extract_scores(scores)
        assert result["综合评分"] == 0
        assert result["综合评分原因"] == ""
        assert result["各维度评分"] == {}

    def test_multiple_flat_dimensions(self):
        """测试多个顶层维度的模板格式"""
        scores = {
            "综合评分": 8,
            "综合评分原因": "综合评价",
            "逻辑一致性": {"score": 9, "analysis": "逻辑清晰"},
            "表达一致性": {"score": 7, "analysis": "表达一般"}
        }
        result = self._extract_scores(scores)
        assert result["综合评分"] == 8
        assert "逻辑一致性" in result["各维度评分"]
        assert "表达一致性" in result["各维度评分"]
        assert result["各维度评分"]["逻辑一致性"]["score"] == 9
        assert result["各维度评分"]["表达一致性"]["score"] == 7
