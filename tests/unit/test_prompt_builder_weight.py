"""提示词权重展示格式单元测试"""
import pytest
from app.services.prompt_builder import PromptBuilder, WEIGHT_MAP

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestPromptBuilderWeightDisplay:
    """权重展示格式测试"""

    def _build_prompt(self, metrics, max_score=10):
        """辅助方法: 从配置构建 PromptBuilder 并返回系统提示词"""
        config = {
            "evaluation_metrics": metrics,
            "max_score": max_score,
            "scene_desc": "测试场景",
        }
        builder = PromptBuilder.from_config(config)
        return builder._build_system_prompt(
            user_input="测试问题",
            ref_answer="参考答案",
            model_output="模型输出",
            dataset_item={},
            payload={},
        )

    def test_text_weight_display(self):
        """权重文字展示: metrics weight='高' → 提示词中包含 '权重: 高(3)'"""
        metrics = [{"name": "准确性", "weight": "高", "description": "内容准确度"}]
        prompt = self._build_prompt(metrics)
        assert "权重: 高(3)" in prompt

    def test_numeric_weight_display(self):
        """权重数值展示: metrics weight=3 → 提示词中包含 '权重: 3'"""
        metrics = [{"name": "准确性", "weight": 3, "description": "内容准确度"}]
        prompt = self._build_prompt(metrics)
        assert "权重: 3" in prompt

    def test_score_range_correct(self):
        """评分范围正确: max_score=5 → 提示词中包含 '0-5'"""
        metrics = [{"name": "准确性", "weight": "高", "description": "内容准确度"}]
        prompt = self._build_prompt(metrics, max_score=5)
        assert "0-5" in prompt

    def test_backend_calculation_hint(self):
        """后端计算提示: 提示词中包含 '综合评分将由系统按权重自动计算'"""
        metrics = [{"name": "准确性", "weight": "高", "description": "内容准确度"}]
        prompt = self._build_prompt(metrics)
        assert "综合评分将由系统按权重自动计算" in prompt
        assert "权重越高(数值越大)的维度对综合评分影响更大" in prompt

    def test_medium_weight_display(self):
        """中等权重展示"""
        metrics = [{"name": "流畅性", "weight": "中"}]
        prompt = self._build_prompt(metrics)
        assert "权重: 中(2)" in prompt

    def test_low_weight_display(self):
        """低权重展示"""
        metrics = [{"name": "创新", "weight": "低"}]
        prompt = self._build_prompt(metrics)
        assert "权重: 低(1)" in prompt
