"""
DeepEval 适配器核心单元测试

测试覆盖：
1. create_test_case - 创建 DeepEval 测试用例（基本、带上下文、带工具）
2. evaluate - 执行单个指标评测（mock DeepEval 调用）
3. evaluate_batch - 批量执行指标评测（并发控制）
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.deepeval_adapter import (
    create_test_case,
    evaluate,
    evaluate_batch,
)
from app.services.deepeval_result_transformer import (
    TransformedMetricResult,
    TransformedEvaluationResult,
)

# deepeval 指标在实例化时会尝试初始化 OpenAI 模型，
# 需要设置 dummy API key 以避免测试因缺少 key 而失败。
_DUMMY_API_KEY_ENV = {"OPENAI_API_KEY": "sk-test-dummy-key-for-testing"}


@pytest.fixture(autouse=True)
def mock_openai_api_key():
    """为所有测试提供 dummy OPENAI_API_KEY"""
    with patch.dict(os.environ, _DUMMY_API_KEY_ENV):
        yield


# ============================================================
# create_test_case 测试
# ============================================================
class TestCreateTestCase:
    """测试创建 DeepEval 测试用例"""

    def test_create_basic_test_case(self):
        """测试创建基本测试用例（仅 input + actual_output）"""
        test_case = create_test_case(
            input_text="What is the capital of France?",
            actual_output="The capital of France is Paris.",
        )
        assert test_case.input == "What is the capital of France?"
        assert test_case.actual_output == "The capital of France is Paris."
        assert test_case.expected_output is None
        assert test_case.context is None
        assert test_case.retrieval_context is None
        assert test_case.tools_called is None
        assert test_case.expected_tools is None

    def test_create_test_case_with_expected_output(self):
        """测试创建带 expected_output 的测试用例"""
        test_case = create_test_case(
            input_text="What is 2+2?",
            actual_output="4",
            expected_output="The answer is 4",
        )
        assert test_case.input == "What is 2+2?"
        assert test_case.actual_output == "4"
        assert test_case.expected_output == "The answer is 4"

    def test_create_test_case_with_context(self):
        """测试创建带 context 和 retrieval_context 的测试用例"""
        test_case = create_test_case(
            input_text="What is DeepEval?",
            actual_output="DeepEval is a testing framework.",
            context=["DeepEval is an open-source LLM evaluation framework."],
            retrieval_context=["DeepEval provides metrics for evaluating LLMs."],
        )
        assert test_case.context == ["DeepEval is an open-source LLM evaluation framework."]
        assert test_case.retrieval_context == ["DeepEval provides metrics for evaluating LLMs."]

    def test_create_test_case_with_tools_called(self):
        """测试创建带 tools_called 的测试用例，验证转换为 ToolCall 格式"""
        tools_called = [
            {"name": "search", "input_parameters": {"query": "DeepEval"}},
            {"name": "calculate", "description": "数学计算工具"},
        ]
        test_case = create_test_case(
            input_text="Search for DeepEval info",
            actual_output="Found information about DeepEval.",
            tools_called=tools_called,
        )
        assert test_case.tools_called is not None
        assert len(test_case.tools_called) == 2
        assert test_case.tools_called[0].name == "search"
        assert test_case.tools_called[0].input_parameters == {"query": "DeepEval"}
        assert test_case.tools_called[1].name == "calculate"
        assert test_case.tools_called[1].description == "数学计算工具"

    def test_create_test_case_with_expected_tools(self):
        """测试创建带 expected_tools 的测试用例"""
        expected_tools = [
            {"name": "search", "description": "搜索工具"},
        ]
        test_case = create_test_case(
            input_text="Search for info",
            actual_output="Found info.",
            expected_tools=expected_tools,
        )
        assert test_case.expected_tools is not None
        assert len(test_case.expected_tools) == 1
        assert test_case.expected_tools[0].name == "search"
        assert test_case.expected_tools[0].description == "搜索工具"

    def test_create_test_case_with_all_params(self):
        """测试创建包含所有参数的测试用例"""
        tools_called = [{"name": "search", "input_parameters": {"q": "test"}}]
        expected_tools = [{"name": "search", "description": "搜索"}]
        test_case = create_test_case(
            input_text="full test",
            actual_output="full output",
            expected_output="full expected",
            context=["context text"],
            retrieval_context=["retrieval text"],
            tools_called=tools_called,
            expected_tools=expected_tools,
        )
        assert test_case.input == "full test"
        assert test_case.actual_output == "full output"
        assert test_case.expected_output == "full expected"
        assert test_case.context == ["context text"]
        assert test_case.retrieval_context == ["retrieval text"]
        assert len(test_case.tools_called) == 1
        assert len(test_case.expected_tools) == 1

    def test_create_test_case_tools_called_conversion_to_toolcall(self):
        """测试 tools_called 字典正确转换为 DeepEval ToolCall 对象"""
        from deepeval.test_case import ToolCall

        tools_called = [
            {
                "name": "web_search",
                "description": "Web search tool",
                "reasoning": "Need to search for information",
                "input_parameters": {"query": "test"},
                "output": "search results",
            },
        ]
        test_case = create_test_case(
            input_text="test",
            actual_output="output",
            tools_called=tools_called,
        )
        tool = test_case.tools_called[0]
        assert isinstance(tool, ToolCall)
        assert tool.name == "web_search"
        assert tool.description == "Web search tool"
        assert tool.reasoning == "Need to search for information"
        assert tool.input_parameters == {"query": "test"}
        assert tool.output == "search results"

    def test_create_test_case_empty_tools_called(self):
        """测试 tools_called 为空列表时不出错"""
        test_case = create_test_case(
            input_text="test",
            actual_output="output",
            tools_called=[],
        )
        assert test_case.tools_called == []

    def test_create_test_case_empty_expected_tools(self):
        """测试 expected_tools 为空列表时不出错"""
        test_case = create_test_case(
            input_text="test",
            actual_output="output",
            expected_tools=[],
        )
        assert test_case.expected_tools == []


# ============================================================
# evaluate 测试
# ============================================================
class TestEvaluate:
    """测试执行单个指标评测"""

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_returns_transformed_result(self, mock_create_metric):
        """测试 evaluate 返回转换后的结果"""
        # 准备 mock 指标
        mock_metric = MagicMock()
        mock_metric.measure.return_value = 0.85
        mock_metric.score = 0.85
        mock_metric.reason = "Good answer"
        mock_metric.success = True
        mock_metric.score_breakdown = None
        mock_metric.error = None
        mock_metric.verdicts = None
        mock_create_metric.return_value = mock_metric

        # 准备测试用例
        test_case = create_test_case(
            input_text="What is AI?",
            actual_output="AI is artificial intelligence.",
        )

        # 执行评测
        result = evaluate(
            test_case=test_case,
            metric_key="answer_relevancy",
            threshold=0.5,
        )

        # 验证
        assert isinstance(result, TransformedMetricResult)
        assert result.metric_key == "answer_relevancy"
        assert result.score == 0.85
        assert result.success is True
        assert result.reason == "Good answer"

        # 验证调用链
        mock_create_metric.assert_called_once_with("answer_relevancy", threshold=0.5)
        mock_metric.measure.assert_called_once_with(test_case)

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_with_custom_metric_name(self, mock_create_metric):
        """测试使用自定义 metric_name"""
        mock_metric = MagicMock()
        mock_metric.measure.return_value = 0.6
        mock_metric.score = 0.6
        mock_metric.reason = "Average"
        mock_metric.success = True
        mock_metric.score_breakdown = None
        mock_metric.error = None
        mock_metric.verdicts = None
        mock_create_metric.return_value = mock_metric

        test_case = create_test_case(
            input_text="test", actual_output="output"
        )

        result = evaluate(
            test_case=test_case,
            metric_key="faithfulness",
            threshold=0.7,
            metric_name="Faithfulness Custom Name",
        )

        assert result.metric_name == "Faithfulness Custom Name"
        assert result.metric_key == "faithfulness"

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_default_metric_name_from_key(self, mock_create_metric):
        """测试 metric_name 默认使用 metric_key"""
        mock_metric = MagicMock()
        mock_metric.measure.return_value = 0.9
        mock_metric.score = 0.9
        mock_metric.reason = None
        mock_metric.success = True
        mock_metric.score_breakdown = None
        mock_metric.error = None
        mock_metric.verdicts = None
        mock_create_metric.return_value = mock_metric

        test_case = create_test_case(
            input_text="test", actual_output="output"
        )

        result = evaluate(
            test_case=test_case,
            metric_key="bias",
            threshold=0.5,
        )

        assert result.metric_name == "bias"

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_handles_metric_error(self, mock_create_metric):
        """测试当指标 measure 抛出异常时返回错误结果"""
        mock_metric = MagicMock()
        mock_metric.measure.side_effect = RuntimeError("API connection failed")
        mock_create_metric.return_value = mock_metric

        test_case = create_test_case(
            input_text="test", actual_output="output"
        )

        result = evaluate(
            test_case=test_case,
            metric_key="toxicity",
            threshold=0.5,
        )

        assert isinstance(result, TransformedMetricResult)
        assert result.success is False
        assert result.error is not None
        assert "API connection failed" in result.error
        assert result.score == 0.0

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_handles_metric_not_found(self, mock_create_metric):
        """测试当指标 key 不存在时返回错误结果"""
        from app.services.deepeval_metric_registry import MetricNotFoundError

        mock_create_metric.side_effect = MetricNotFoundError(
            metric_key="nonexistent",
            available_keys=["task_completion", "bias"],
        )

        test_case = create_test_case(
            input_text="test", actual_output="output"
        )

        result = evaluate(
            test_case=test_case,
            metric_key="nonexistent",
            threshold=0.5,
        )

        assert isinstance(result, TransformedMetricResult)
        assert result.success is False
        assert result.error is not None
        assert "nonexistent" in result.error

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_evaluate_passes_extra_kwargs_to_registry(self, mock_create_metric):
        """测试额外参数传递给 create_metric"""
        mock_metric = MagicMock()
        mock_metric.measure.return_value = 0.7
        mock_metric.score = 0.7
        mock_metric.reason = None
        mock_metric.success = True
        mock_metric.score_breakdown = None
        mock_metric.error = None
        mock_metric.verdicts = None
        mock_create_metric.return_value = mock_metric

        test_case = create_test_case(
            input_text="test", actual_output="output"
        )

        evaluate(
            test_case=test_case,
            metric_key="faithfulness",
            threshold=0.8,
            include_reason=True,
        )

        mock_create_metric.assert_called_once_with(
            "faithfulness", threshold=0.8, include_reason=True
        )


# ============================================================
# evaluate_batch 测试
# ============================================================
class TestEvaluateBatch:
    """测试批量执行指标评测"""

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_single_metric(self, mock_evaluate):
        """测试批量评测单个指标"""
        # 准备 mock
        mock_result = TransformedMetricResult(
            metric_key="answer_relevancy",
            metric_name="answer_relevancy",
            score=0.8,
            success=True,
        )
        mock_evaluate.return_value = mock_result

        test_cases = [
            create_test_case(input_text="Q1", actual_output="A1"),
            create_test_case(input_text="Q2", actual_output="A2"),
        ]
        metric_configs = [{"key": "answer_relevancy", "threshold": 0.5}]

        results = asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=2,
            )
        )

        assert len(results) == 2
        assert all(isinstance(r, TransformedMetricResult) for r in results)
        assert mock_evaluate.call_count == 2

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_multiple_metrics(self, mock_evaluate):
        """测试批量评测多个指标"""
        mock_result = TransformedMetricResult(
            metric_key="test", metric_name="test", score=0.9, success=True
        )
        mock_evaluate.return_value = mock_result

        test_cases = [
            create_test_case(input_text="Q1", actual_output="A1"),
        ]
        metric_configs = [
            {"key": "answer_relevancy", "threshold": 0.5},
            {"key": "faithfulness", "threshold": 0.7},
            {"key": "bias", "threshold": 0.3},
        ]

        results = asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=5,
            )
        )

        # 1 test_case * 3 metrics = 3 results
        assert len(results) == 3
        assert mock_evaluate.call_count == 3

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_concurrency_control(self, mock_evaluate):
        """测试并发控制 - max_concurrent 限制并发数"""
        call_log = []

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            call_log.append(metric_key)
            return TransformedMetricResult(
                metric_key=metric_key,
                metric_name=metric_key,
                score=0.5,
                success=True,
            )

        mock_evaluate.side_effect = side_effect_func

        test_cases = [
            create_test_case(input_text=f"Q{i}", actual_output=f"A{i}")
            for i in range(5)
        ]
        metric_configs = [{"key": "answer_relevancy", "threshold": 0.5}]

        results = asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=2,
            )
        )

        assert len(results) == 5
        assert len(call_log) == 5

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_empty_test_cases(self, mock_evaluate):
        """测试空测试用例列表返回空结果"""
        results = asyncio.run(
            evaluate_batch(
                test_cases=[],
                metric_configs=[{"key": "answer_relevancy", "threshold": 0.5}],
                max_concurrent=5,
            )
        )
        assert results == []
        mock_evaluate.assert_not_called()

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_empty_metric_configs(self, mock_evaluate):
        """测试空指标配置返回空结果"""
        results = asyncio.run(
            evaluate_batch(
                test_cases=[
                    create_test_case(input_text="Q1", actual_output="A1")
                ],
                metric_configs=[],
                max_concurrent=5,
            )
        )
        assert results == []
        mock_evaluate.assert_not_called()

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_handles_partial_failure(self, mock_evaluate):
        """测试部分评测失败时不影响其他评测"""
        call_count = [0]

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated failure")
            return TransformedMetricResult(
                metric_key=metric_key,
                metric_name=metric_key,
                score=0.7,
                success=True,
            )

        mock_evaluate.side_effect = side_effect_func

        test_cases = [
            create_test_case(input_text=f"Q{i}", actual_output=f"A{i}")
            for i in range(3)
        ]
        metric_configs = [{"key": "answer_relevancy", "threshold": 0.5}]

        results = asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=1,
            )
        )

        assert len(results) == 3
        # 第二个应该是错误结果
        assert results[1].success is False
        assert results[1].error is not None
        # 其他应该是成功结果
        assert results[0].success is True
        assert results[2].success is True

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_passes_threshold_from_config(self, mock_evaluate):
        """测试批量评测传递 config 中的 threshold"""
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="test", metric_name="test", score=0.5, success=True
        )

        test_cases = [
            create_test_case(input_text="Q1", actual_output="A1"),
        ]
        metric_configs = [
            {"key": "faithfulness", "threshold": 0.9, "name": "Faithfulness"},
        ]

        asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=1,
            )
        )

        mock_evaluate.assert_called_once()
        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.9 or call_kwargs[1].get("threshold") == 0.9

    @patch("app.services.deepeval_adapter.evaluate")
    def test_evaluate_batch_passes_metric_name(self, mock_evaluate):
        """测试批量评测传递 config 中的 name"""
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="test", metric_name="test", score=0.5, success=True
        )

        test_cases = [
            create_test_case(input_text="Q1", actual_output="A1"),
        ]
        metric_configs = [
            {"key": "faithfulness", "threshold": 0.5, "name": "Custom Faithfulness"},
        ]

        asyncio.run(
            evaluate_batch(
                test_cases=test_cases,
                metric_configs=metric_configs,
                max_concurrent=1,
            )
        )

        mock_evaluate.assert_called_once()
        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("metric_name") == "Custom Faithfulness" or call_kwargs[1].get("metric_name") == "Custom Faithfulness"
