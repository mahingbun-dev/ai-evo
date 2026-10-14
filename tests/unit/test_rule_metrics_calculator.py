"""RuleMetricsCalculator 单元测试"""
import pytest
from app.services.rule_metrics_calculator import RuleMetricsCalculator

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestRuleMetricsCalculator:
    """RuleMetricsCalculator 测试"""

    # --- 空列表或单条文本 ---
    def test_empty_list(self):
        result = RuleMetricsCalculator.calculate_all([])
        assert result["text_similarity"] == 0.0
        assert result["format_consistency"] == 0.0
        assert result["structure_consistency"] == 0.0

    def test_single_text(self):
        result = RuleMetricsCalculator.calculate_all(["hello world"])
        assert result["text_similarity"] == 0.0
        assert result["format_consistency"] == 0.0
        assert result["structure_consistency"] == 0.0

    # --- 完全相同纯 JSON 文本 x3 ---
    def test_identical_json(self):
        texts = ['{"a": 1, "b": 2}', '{"a": 1, "b": 2}', '{"a": 1, "b": 2}']
        result = RuleMetricsCalculator.calculate_all(texts)
        assert result["text_similarity"] == 1.0
        assert result["format_consistency"] == 1.0
        assert result["structure_consistency"] == 1.0

    # --- 相同结构不同值的 JSON 文本 x3 ---
    def test_same_structure_different_values(self):
        texts = ['{"a": 1, "b": 2}', '{"a": 3, "b": 4}', '{"a": 5, "b": 6}']
        result = RuleMetricsCalculator.calculate_all(texts)
        assert result["text_similarity"] < 1.0
        assert result["format_consistency"] == 1.0
        assert result["structure_consistency"] == 1.0

    # --- 混合格式 (JSON + Markdown + Plain) ---
    def test_mixed_format(self):
        texts = [
            '{"a": 1}',
            '# 标号\n- 列表项1\n- 列表项2',
            'plain text here',
        ]
        result = RuleMetricsCalculator.calculate_all(texts)
        assert result["format_consistency"] < 1.0

    # --- 中文文本相似度 ---
    def test_chinese_similarity(self):
        texts = ['今天 天气好', '今天 风很大']
        similarity = RuleMetricsCalculator.calculate_text_similarity(texts)
        assert 0.0 < similarity < 1.0

    def test_chinese_identical(self):
        texts = ['今天天气真好', '今天天气真好']
        similarity = RuleMetricsCalculator.calculate_text_similarity(texts)
        assert similarity == 1.0

    # --- calculate_all 返回包含全部 3 个 key 且值与单独调用一致 ---
    def test_calculate_all_keys(self):
        texts = ['{"x": 1}', '{"x": 2}', '{"x": 3}']
        result = RuleMetricsCalculator.calculate_all(texts)
        assert set(result.keys()) == {"text_similarity", "format_consistency", "structure_consistency"}
        assert result["text_similarity"] == round(RuleMetricsCalculator.calculate_text_similarity(texts), 4)
        assert result["format_consistency"] == round(RuleMetricsCalculator.calculate_format_consistency(texts), 4)
        assert result["structure_consistency"] == round(RuleMetricsCalculator.calculate_structure_consistency(texts), 4)

    # --- Markdown JSON 格式 ---
    def test_markdown_json_format(self):
        texts = [
            '```json\n{"a": 1}\n```',
            '```json\n{"b": 2}\n```',
        ]
        result = RuleMetricsCalculator.calculate_all(texts)
        assert result["format_consistency"] == 1.0

    # --- _extract_json_from_markdown ---
    def test_extract_json_from_markdown(self):
        text = '```json\n{"score": 8}\n```'
        extracted = RuleMetricsCalculator._extract_json_from_markdown(text)
        assert extracted == '{"score": 8}'

    def test_extract_json_from_markdown_no_block(self):
        text = 'plain text'
        extracted = RuleMetricsCalculator._extract_json_from_markdown(text)
        assert extracted == 'plain text'
