"""
耗时评测执行器单元测试
"""
import pytest
import time
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class MockModelCallerSync:
    """Mock ModelCallerSync for testing"""

    def __init__(self, latency=0.1):
        self.latency = latency

    def call(self, messages, model_config, runtime_variables):
        time.sleep(self.latency)
        return {
            "success": True,
            "content": "Mock response",
            "raw": {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                }
            }
        }


class MockTask:
    """Mock task object for testing"""

    def __init__(self, threshold=10.0):
        self.latency_config = {"threshold": threshold}
        self.model_config = {
            "provider": "openai",
            "model_type": "gpt-4",
            "endpoint": "https://api.openai.com/v1",
            "api_key": "test-key",
        }
        self.model_provider = None


class TestEvaluationExecutorLatency:
    """EvaluationExecutor 耗时评测方法单元测试"""

    def test_evaluate_single_latency_returns_duration(self):
        """验证返回结果包含 duration、passed、threshold 字段"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.5)):
            result = executor._evaluate_single_latency(payload, MockTask(threshold=10.0))

        assert "duration" in result, "结果应包含 duration 字段"
        assert "passed" in result, "结果应包含 passed 字段"
        assert "threshold" in result, "结果应包含 threshold 字段"
        assert isinstance(result["duration"], float), "duration 应为浮点数"
        assert result["threshold"] == 10.0

    def test_evaluate_single_latency_passed(self):
        """当模型响应时间 < 阈值时 → passed == True"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        # 使用极短的 latency 确保通过
        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.01)):
            result = executor._evaluate_single_latency(
                {"idx": 0, "user_input": "test question"},
                MockTask(threshold=10.0)
            )

        assert result["success"] is True, "评测应该成功"
        assert result["passed"] is True, "响应时间远小于阈值，应该 passed"
        assert result["duration"] < 10.0

    def test_evaluate_single_latency_failed(self):
        """当模型响应时间 > 阈值时 → passed == False"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        # 使用较长的 latency 确保超过阈值
        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=5.0)):
            result = executor._evaluate_single_latency(
                {"idx": 0, "user_input": "test question"},
                MockTask(threshold=0.1)  # 阈值设置为 0.1 秒
            )

        assert result["success"] is True, "评测应该成功"
        assert result["passed"] is False, "响应时间大于阈值，应该 failed"
        assert result["duration"] > 0.1

    def test_execute_batch_latency_returns_list(self):
        """execute_batch_latency() 返回列表且元素数量与 payloads 一致"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task", max_workers=2)
        payloads = [
            {"idx": 0, "user_input": "question 1"},
            {"idx": 1, "user_input": "question 2"},
            {"idx": 2, "user_input": "question 3"},
        ]

        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.01)):
            results = executor.execute_batch_latency(payloads, MockTask(threshold=10.0))

        assert isinstance(results, list), "结果应为列表"
        assert len(results) == len(payloads), f"结果数量应与 payloads 一致，实际: {len(results)}, 预期: {len(payloads)}"

    def test_execute_batch_latency_preserves_order(self):
        """验证结果按 idx 排序"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task", max_workers=4)
        payloads = [
            {"idx": 0, "user_input": "question 0"},
            {"idx": 1, "user_input": "question 1"},
            {"idx": 2, "user_input": "question 2"},
            {"idx": 3, "user_input": "question 3"},
        ]

        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.01)):
            results = executor.execute_batch_latency(payloads, MockTask(threshold=10.0))

        assert results[0]["idx"] == 0
        assert results[1]["idx"] == 1
        assert results[2]["idx"] == 2
        assert results[3]["idx"] == 3

    def test_evaluate_single_latency_with_tokens(self):
        """验证 token 使用量被正确提取"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        mock_caller = MockModelCallerSync(latency=0.1)
        with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
            result = executor._evaluate_single_latency(payload, MockTask(threshold=10.0))

        assert result["success"] is True
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_evaluate_single_latency_model_call_failure(self):
        """模型调用失败时返回 success=False"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        class FailedModelCaller:
            def call(self, messages, model_config, runtime_variables):
                return {"success": False, "error": "API error"}

        with patch('app.services.model_caller.ModelCallerSync', return_value=FailedModelCaller()):
            result = executor._evaluate_single_latency(payload, MockTask(threshold=10.0))

        assert result["success"] is False
        assert "error" in result

    def test_execute_batch_latency_all_pass(self):
        """批量执行时所有请求都通过"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task", max_workers=2)
        payloads = [
            {"idx": i, "user_input": f"question {i}"}
            for i in range(5)
        ]

        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.01)):
            results = executor.execute_batch_latency(payloads, MockTask(threshold=10.0))

        assert len(results) == 5
        for result in results:
            assert result["success"] is True
            assert result["passed"] is True

    def test_execute_batch_latency_with_repeat_count(self):
        """验证 repeat_count > 1 时每个样本执行多次"""
        from app.services.evaluation_executor import EvaluationExecutor

        class MockTaskWithRepeatCount:
            def __init__(self, threshold=10.0, repeat_count=2):
                self.latency_config = {"threshold": threshold, "repeat_count": repeat_count}
                self.model_config = {
                    "provider": "openai",
                    "model_type": "gpt-4",
                    "endpoint": "https://api.openai.com/v1",
                    "api_key": "test-key",
                }
                self.model_provider = None

        executor = EvaluationExecutor(task_id="test-task", max_workers=4)
        payloads = [
            {"idx": 0, "user_input": "question 0"},
            {"idx": 1, "user_input": "question 1"},
        ]

        with patch('app.services.model_caller.ModelCallerSync', return_value=MockModelCallerSync(latency=0.01)):
            results = executor.execute_batch_latency(payloads, MockTaskWithRepeatCount(threshold=10.0, repeat_count=2))

        # 2 samples * 2 repetitions = 4 results
        assert len(results) == 4, f"Expected 4 results, got {len(results)}"

        # Count results per sample_idx
        idx_counts = {}
        for r in results:
            idx = r["idx"]
            idx_counts[idx] = idx_counts.get(idx, 0) + 1

        # Each sample should appear exactly 2 times
        assert idx_counts.get(0, 0) == 2, f"Sample 0 should appear 2 times, got {idx_counts.get(0, 0)}"
        assert idx_counts.get(1, 0) == 2, f"Sample 1 should appear 2 times, got {idx_counts.get(1, 0)}"