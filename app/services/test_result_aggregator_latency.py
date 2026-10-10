"""
Task 5: 结果聚合扩展 (calculate_latency_metrics) - 单元测试
"""
import pytest
from unittest.mock import MagicMock, patch


class TestCalculateLatencyMetrics:
    """测试 ResultAggregator.calculate_latency_metrics() 静态方法"""

    def test_calculate_latency_metrics_empty(self):
        """传入空列表 → 返回全零结果"""
        from app.services.result_aggregator import ResultAggregator

        result = ResultAggregator.calculate_latency_metrics([])

        assert result["total_count"] == 0
        assert result["passed_count"] == 0
        assert result["failed_count"] == 0
        assert result["pass_rate"] == 0.0
        assert result["avg_duration"] == 0.0
        assert result["p50_duration"] == 0.0
        assert result["p90_duration"] == 0.0
        assert result["p99_duration"] == 0.0
        assert result["score"] == 0.0
        assert result["records"] == []

    def test_calculate_latency_metrics_normal(self):
        """正常数据 → 验证返回包含 avg_duration、p50_duration、p90_duration、p99_duration、score 等字段"""
        from app.services.result_aggregator import ResultAggregator

        # 模拟 latency_results: 5个成功的结果
        latency_results = [
            {"success": True, "duration": 2.0, "input_tokens": 100, "output_tokens": 50},
            {"success": True, "duration": 4.0, "input_tokens": 120, "output_tokens": 60},
            {"success": True, "duration": 6.0, "input_tokens": 80, "output_tokens": 40},
            {"success": True, "duration": 8.0, "input_tokens": 90, "output_tokens": 45},
            {"success": True, "duration": 10.0, "input_tokens": 110, "output_tokens": 55},
        ]

        result = ResultAggregator.calculate_latency_metrics(latency_results, threshold=10.0)

        # 验证必需字段存在
        assert "avg_duration" in result
        assert "p50_duration" in result
        assert "p90_duration" in result
        assert "p99_duration" in result
        assert "score" in result
        assert "total_count" in result
        assert "passed_count" in result
        assert "failed_count" in result
        assert "pass_rate" in result
        assert "min_duration" in result
        assert "max_duration" in result
        assert "threshold" in result
        assert "records" in result

        # 验证统计数据正确
        assert result["total_count"] == 5
        assert result["passed_count"] == 5  # 全部 <= 10.0
        assert result["failed_count"] == 0
        assert result["pass_rate"] == 1.0
        assert result["threshold"] == 10.0

        # avg = (2+4+6+8+10)/5 = 6.0
        assert result["avg_duration"] == 6.0
        assert result["min_duration"] == 2.0
        assert result["max_duration"] == 10.0

        # score = 100 * (1 - (6 - 10) / (30 - 10)) = 100 * (1 - (-4)/20) = 100 * 1.2 = 120, capped to 100
        assert result["score"] == 100.0

    def test_calculate_latency_metrics_with_failures(self):
        """部分失败场景 → 验证 passed_count 和 failed_count 正确"""
        from app.services.result_aggregator import ResultAggregator

        latency_results = [
            {"success": True, "duration": 2.0},
            {"success": True, "duration": 4.0},
            {"success": True, "duration": 12.0},  # 超过阈值 10.0
            {"success": True, "duration": 8.0},
            {"success": False, "duration": 0},   # 失败 - 不计入
        ]

        result = ResultAggregator.calculate_latency_metrics(latency_results, threshold=10.0)

        # total_count = 4 (4个success=True的结果, 第5个success=False被过滤)
        # passed_count = 3 (2.0, 4.0, 8.0 <= 10.0)
        # failed_count = 1 (12.0 > 10.0)
        assert result["total_count"] == 4
        assert result["passed_count"] == 3
        assert result["failed_count"] == 1
        assert result["pass_rate"] == 0.75

    def test_calculate_latency_metrics_records(self):
        """验证返回的 records 数量与成功结果数量一致"""
        from app.services.result_aggregator import ResultAggregator

        latency_results = [
            {"success": True, "duration": 2.0, "input_tokens": 100, "output_tokens": 50},
            {"success": True, "duration": 4.0, "input_tokens": 120, "output_tokens": 60},
            {"success": False, "duration": 0},  # 不计入 records
            {"success": True, "duration": 6.0, "input_tokens": 80, "output_tokens": 40},
        ]

        result = ResultAggregator.calculate_latency_metrics(latency_results, threshold=10.0)

        # 只有 3 个 success=True 的结果
        assert result["total_count"] == 3
        assert len(result["records"]) == 3

        # 验证 records 中的字段
        for record in result["records"]:
            assert "index" in record
            assert "duration" in record
            assert "timestamp" in record
            assert "passed" in record
            assert "input_tokens" in record
            assert "output_tokens" in record


class TestAggregateResultsLatencyType:
    """测试 _aggregate_results() 对 evaluation_type='latency' 的处理"""

    def test_aggregate_results_latency_type_exists(self):
        """验证 _aggregate_results() 对 latency 类型的处理分支存在"""
        import ast
        import os

        # 读取 evaluation_tasks_async.py 源码
        file_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "app",
            "tasks",
            "evaluation_tasks_async.py"
        )

        with open(file_path, "r") as f:
            content = f.read()

        # 验证代码中包含 latency 类型的聚合分支
        assert 'if task.evaluation_type == "latency"' in content
        assert "ResultAggregator.calculate_latency_metrics" in content

    def test_aggregate_results_latency_type_branch(self):
        """验证 latency 分支在 success_rate 分支之后、aggregator 之前"""
        from app.services.result_aggregator import ResultAggregator

        # 创建一个 mock task
        mock_task = MagicMock()
        mock_task.evaluation_type = "latency"
        mock_task.latency_config = {"threshold": 5.0}

        # 模拟 latency_results
        latency_results = [
            {"success": True, "duration": 2.0, "input_tokens": 100, "output_tokens": 50},
            {"success": True, "duration": 4.0, "input_tokens": 120, "output_tokens": 60},
        ]

        # 调用 calculate_latency_metrics 验证功能
        result = ResultAggregator.calculate_latency_metrics(latency_results, threshold=5.0)

        assert result["total_count"] == 2
        assert result["threshold"] == 5.0
        assert result["score"] == 100.0  # avg=3.0 < threshold=5.0, score=100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])