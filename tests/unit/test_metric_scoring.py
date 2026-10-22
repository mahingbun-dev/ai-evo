"""维度最高分与分数归一化共享助手单元测试（metric_scoring）"""
import pytest

from app.services.metric_scoring import (
    clamp_to_metric_max,
    format_score_range,
    has_custom_dimension_max,
    metric_max_score,
    normalize_dimension_score,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestMetricMaxScore:
    """metric_max_score 回退口径测试"""

    def test_explicit_max(self):
        assert metric_max_score({"name": "A", "max_score": 5}, 10) == 5.0

    def test_missing_fallback_task_max(self):
        assert metric_max_score({"name": "A"}, 10) == 10.0

    def test_none_metric_fallback(self):
        assert metric_max_score(None, 10) == 10.0

    def test_invalid_string_fallback(self):
        assert metric_max_score({"name": "A", "max_score": "abc"}, 10) == 10.0

    def test_zero_and_negative_fallback(self):
        assert metric_max_score({"name": "A", "max_score": 0}, 10) == 10.0
        assert metric_max_score({"name": "A", "max_score": -3}, 10) == 10.0

    def test_task_max_none_defaults_ten(self):
        assert metric_max_score({"name": "A"}, None) == 10.0


class TestClampToMetricMax:
    """clamp_to_metric_max 量程夹取测试"""

    def test_within_range_unchanged(self):
        assert clamp_to_metric_max(4, {"max_score": 5}, 10) == 4.0

    def test_above_dim_max_clamped(self):
        """裁判违反指令给了超维度最高分的分 → 夹到维度最高分"""
        assert clamp_to_metric_max(10, {"max_score": 5}, 10) == 5.0

    def test_negative_clamped_to_zero(self):
        assert clamp_to_metric_max(-2, {"max_score": 5}, 10) == 0.0

    def test_invalid_score_zero(self):
        assert clamp_to_metric_max("abc", {"max_score": 5}, 10) == 0.0

    def test_no_metric_max_clamps_to_task_max(self):
        assert clamp_to_metric_max(15, {}, 10) == 10.0


class TestNormalizeDimensionScore:
    """normalize_dimension_score 归一化测试"""

    def test_identity_when_no_custom_max(self):
        """维度未配置最高分 → 恒等变换（存量任务口径不变）"""
        assert normalize_dimension_score(7, {"name": "A"}, 10) == pytest.approx(7.0)

    def test_full_marks_low_range(self):
        """最高分 5 打 5 → 归一化 10（满分表现等价）"""
        assert normalize_dimension_score(5, {"max_score": 5}, 10) == pytest.approx(10.0)

    def test_half_marks_low_range(self):
        assert normalize_dimension_score(2.5, {"max_score": 5}, 10) == pytest.approx(5.0)

    def test_over_range_clamped_then_normalized(self):
        """超量程先夹到 5 再归一化为 10，不会溢出任务满分"""
        assert normalize_dimension_score(10, {"max_score": 5}, 10) == pytest.approx(10.0)


class TestHasCustomDimensionMax:
    """has_custom_dimension_max 守卫测试"""

    def test_no_metrics_false(self):
        assert has_custom_dimension_max([], 10) is False
        assert has_custom_dimension_max(None, 10) is False

    def test_all_default_false(self):
        """全部维度等于任务满分（前端缺省回填场景）→ 视为未配置"""
        metrics = [{"name": "A", "max_score": 10}, {"name": "B", "max_score": 10}]
        assert has_custom_dimension_max(metrics, 10) is False

    def test_any_custom_true(self):
        metrics = [{"name": "A", "max_score": 10}, {"name": "B", "max_score": 5}]
        assert has_custom_dimension_max(metrics, 10) is True

    def test_invalid_values_ignored(self):
        metrics = [{"name": "A", "max_score": "abc"}, {"name": "B", "max_score": 0}, "junk"]
        assert has_custom_dimension_max(metrics, 10) is False


class TestFormatScoreRange:
    def test_integer_float(self):
        assert format_score_range(10.0) == "10"

    def test_decimal(self):
        assert format_score_range(5.5) == "5.5"

    def test_invalid_defaults_ten(self):
        assert format_score_range(None) == "10"
