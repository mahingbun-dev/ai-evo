"""InstructionBuilder {{steps}} 填充契约测试（契约 v3 §四）

{{steps}} 占位符保留，填充内容改为从 evaluation_metrics 派生的有序维度序列；
无维度配置时回退旧版 steps / 默认步骤，保证空配置链路可用。
"""
import pytest
from app.services.instruction_builder import InstructionBuilder


class TestStepsDerivedFromMetrics:
    """{{steps}} 从 evaluation_metrics 派生"""

    def test_steps_derived_from_metrics_order(self):
        """有维度配置 → {{steps}} 填充 'N. 维度名：维度定义' 有序序列"""
        config = {
            "scene": "通用",
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高", "description": "内容准确"},
                {"name": "完整性", "weight": "中", "description": "覆盖完整"},
            ],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)

        assert "1. 准确性：内容准确" in prompt
        assert "2. 完整性：覆盖完整" in prompt
        # 占位符已被替换
        assert "{{steps}}" not in prompt

    def test_legacy_steps_ignored_when_metrics_present(self):
        """有维度配置时旧版 config['steps'] 不再参与填充"""
        config = {
            "scene": "通用",
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高", "description": "内容准确"},
            ],
            "steps": ["旧步骤1", "旧步骤2"],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)

        assert "1. 准确性：内容准确" in prompt
        assert "旧步骤1" not in prompt
        assert "旧步骤2" not in prompt

    def test_metric_without_description(self):
        """维度无描述 → 步骤行以冒号结尾，仍占序号"""
        config = {
            "scene": "通用",
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高", "description": ""},
                {"name": "完整性", "weight": "中", "description": "覆盖完整"},
            ],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)

        assert "1. 准确性：" in prompt
        assert "2. 完整性：覆盖完整" in prompt

    def test_derivation_format_consistent_with_graph_state(self):
        """派生格式与 judge_graph_state.derive_scoring_steps 一致"""
        from app.services.judge_graph_state import derive_scoring_steps

        metrics = [
            {"name": "准确性", "weight": "高", "description": "内容准确"},
            {"name": "完整性", "weight": "中", "description": "覆盖完整"},
        ]
        builder = InstructionBuilder()
        steps_text = builder._build_steps_from_metrics(metrics)

        assert steps_text == "\n".join(derive_scoring_steps(metrics))


class TestStepsFallback:
    """无维度配置时的回退行为"""

    def test_fallback_to_legacy_steps_when_no_metrics(self):
        """无维度 + 旧版 steps → 沿用旧版步骤填充（过渡期兼容）"""
        config = {
            "scene": "通用",
            "evaluation_metrics": [],
            "steps": ["1. 阅读理解", "2. 逐维度评估"],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)

        assert "1. 阅读理解" in prompt
        assert "2. 逐维度评估" in prompt

    def test_default_steps_when_no_metrics_no_steps(self):
        """无维度且无 steps → 使用默认步骤文本"""
        config = {
            "scene": "通用",
            "evaluation_metrics": [],
            "steps": [],
        }
        builder = InstructionBuilder()
        prompt = builder.build(config)

        assert "1. 阅读理解" in prompt
        assert "2. 评估回复" in prompt
        assert "3. 综合评分" in prompt
