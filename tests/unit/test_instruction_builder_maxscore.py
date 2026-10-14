"""InstructionBuilder max_score 逻辑修复单元测试"""
import pytest
from app.services.instruction_builder import InstructionBuilder

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestInstructionBuilderMaxScore:
    """验证 max_score 不再被维度级值覆盖"""

    def test_max_score_from_config_takes_precedence(self):
        """config max_score=5，维度 max_score=10 → 最终 max_score=5"""
        config = {
            "max_score": 5,
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高", "max_score": 10},
                {"name": "流畅性", "weight": "中", "max_score": 10},
            ],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)
        # prompt 中应包含 "0 至 5" 而非 "0 至 10"
        assert "0 至 5" in prompt
        assert "0 至 10" not in prompt

    def test_default_max_score(self):
        """config 无 max_score → 默认 10"""
        config = {
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高"},
            ],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)
        assert "0 至 10" in prompt

    def test_no_metrics_uses_config_max_score(self):
        """config 无 metrics → 使用 config max_score"""
        config = {
            "max_score": 7,
            "evaluation_metrics": [],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)
        assert "0 至 7" in prompt
