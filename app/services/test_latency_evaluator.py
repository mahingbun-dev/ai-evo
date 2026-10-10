"""
耗时评测服务单元测试
"""
import pytest
from app.services.latency_evaluator import LatencyEvaluator, LatencyRecord, LatencyResult


class TestLatencyEvaluator:
    """LatencyEvaluator 单元测试"""

    def test_evaluate_with_empty_durations(self):
        """测试空列表 → 返回全零结果"""
        evaluator = LatencyEvaluator(threshold=10.0)
        result = evaluator.evaluate([])

        assert result.total_count == 0
        assert result.passed_count == 0
        assert result.failed_count == 0
        assert result.pass_rate == 0.0
        assert result.avg_duration == 0.0
        assert result.min_duration == 0.0
        assert result.max_duration == 0.0
        assert result.p50_duration == 0.0
        assert result.p90_duration == 0.0
        assert result.p99_duration == 0.0
        assert result.score == 0.0
        assert result.threshold == 10.0
        assert result.records == []

    def test_evaluate_all_pass(self):
        """测试所有耗时都低于阈值 → passed_count == total_count, pass_rate == 1.0"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = evaluator.evaluate(durations)

        assert result.total_count == 5
        assert result.passed_count == 5
        assert result.failed_count == 0
        assert result.pass_rate == 1.0

    def test_evaluate_all_fail(self):
        """测试所有耗时都超过阈值 → passed_count == 0, pass_rate == 0.0"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [15.0, 20.0, 25.0, 30.0, 35.0]
        result = evaluator.evaluate(durations)

        assert result.total_count == 5
        assert result.passed_count == 0
        assert result.failed_count == 5
        assert result.pass_rate == 0.0

    def test_evaluate_score_full_marks(self):
        """测试当 avg_duration <= threshold 时 → score == 100.0"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [5.0, 6.0, 7.0, 8.0, 9.0]  # avg = 7.0 <= 10.0
        result = evaluator.evaluate(durations)

        assert result.score == 100.0

    def test_evaluate_score_zero(self):
        """测试当 avg_duration >= threshold * 3 时 → score == 0.0"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [35.0, 35.0, 35.0, 35.0, 35.0]  # avg = 35.0 >= 30.0
        result = evaluator.evaluate(durations)

        assert result.score == 0.0

    def test_evaluate_score_linear_interpolation(self):
        """测试当 threshold < avg_duration < threshold * 3 时 → 0 < score < 100"""
        evaluator = LatencyEvaluator(threshold=10.0)
        # threshold=10, threshold*3=30, avg should be between 10 and 30
        durations = [15.0, 18.0, 20.0, 22.0, 25.0]  # avg = 20.0
        result = evaluator.evaluate(durations)

        assert 0.0 < result.score < 100.0
        # Linear interpolation: 100 * (1 - (20-10)/(30-10)) = 100 * (1 - 0.5) = 50.0
        assert result.score == 50.0

    def test_evaluate_percentiles(self):
        """测试百分位数计算正确"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = list(range(1, 101))  # 1 to 100
        result = evaluator.evaluate(durations)

        # p50 = sorted_data[int(100*50/100)] = sorted_data[50] = 51
        assert result.p50_duration == 51
        # p90 = sorted_data[int(100*90/100)] = sorted_data[90] = 91
        assert result.p90_duration == 91
        # p99 = sorted_data[int(100*99/100)] = sorted_data[99] = 100
        assert result.p99_duration == 100

    def test_evaluate_records(self):
        """测试返回的 records 数量与 durations 一致，且每个 record 的 passed 字段正确"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [5.0, 15.0, 8.0, 20.0, 3.0]
        result = evaluator.evaluate(durations)

        assert len(result.records) == len(durations)

        # Check each record's passed field
        expected_passed = [d <= 10.0 for d in durations]
        for i, record in enumerate(result.records):
            assert record.index == i + 1
            assert record.duration == durations[i]
            assert record.passed == expected_passed[i]

    def test_evaluate_with_tokens(self):
        """测试包含 input_tokens 和 output_tokens 的情况"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [5.0, 6.0, 7.0]
        input_tokens = [100, 200, 300]
        output_tokens = [50, 100, 150]
        result = evaluator.evaluate(durations, input_tokens, output_tokens)

        assert len(result.records) == 3
        assert result.records[0].input_tokens == 100
        assert result.records[0].output_tokens == 50
        assert result.records[1].input_tokens == 200
        assert result.records[1].output_tokens == 100
        assert result.records[2].input_tokens == 300
        assert result.records[2].output_tokens == 150

    def test_evaluate_with_partial_tokens(self):
        """测试 tokens 列表不完整时"""
        evaluator = LatencyEvaluator(threshold=10.0)
        durations = [5.0, 6.0, 7.0]
        input_tokens = [100, 200]  # 少一个
        result = evaluator.evaluate(durations, input_tokens, None)

        assert result.records[0].input_tokens == 100
        assert result.records[1].input_tokens == 200
        assert result.records[2].input_tokens is None
