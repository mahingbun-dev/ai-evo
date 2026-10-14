"""
Agent 评测器单元测试

测试覆盖：
1. evaluate_task_completion - 评测任务完成度
2. evaluate_tool_correctness - 评测工具调用正确性
3. evaluate_step_efficiency - 评测步骤效率
4. evaluate_all - 综合评测（并行执行所有指标）
5. evaluate_task_completion_batch - 批量评测任务完成度（并发控制）
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.deepeval_result_transformer import TransformedMetricResult

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ============================================================
# evaluate_task_completion 测试
# ============================================================
class TestEvaluateTaskCompletion:
    """测试任务完成度评测"""

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_task_completion_basic(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试基本的任务完成度评测调用"""
        from app.services.agent_evaluator import AgentEvaluator

        # 准备 mock
        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.9,
            success=True,
            reason="任务成功完成",
        )
        mock_evaluate.return_value = expected_result

        # 执行评测
        result = AgentEvaluator.evaluate_task_completion(
            task="搜索北京天气",
            input_text="请帮我搜索北京今天的天气",
            actual_output="北京今天晴，温度25度",
            expected_output="北京天气信息",
            threshold=0.7,
        )

        # 验证 create_test_case 调用
        mock_create_test_case.assert_called_once_with(
            input_text="请帮我搜索北京今天的天气",
            actual_output="北京今天晴，温度25度",
            expected_output="北京天气信息",
        )

        # 验证 evaluate 调用
        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="task_completion",
            threshold=0.7,
            metric_name="任务完成度",
        )

        # 验证返回结果
        assert result == expected_result
        assert result.score == 0.9
        assert result.success is True

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_task_completion_default_threshold(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试任务完成度评测使用默认阈值"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.6,
            success=True,
        )

        AgentEvaluator.evaluate_task_completion(
            task="测试任务",
            input_text="输入",
            actual_output="输出",
        )

        # 验证默认 threshold=0.5
        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5 or call_kwargs[1].get("threshold") == 0.5

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_task_completion_without_expected_output(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试不传 expected_output 时的任务完成度评测"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.5,
            success=False,
        )

        AgentEvaluator.evaluate_task_completion(
            task="测试任务",
            input_text="输入",
            actual_output="输出",
        )

        # 验证 create_test_case 传入 expected_output=None
        call_kwargs = mock_create_test_case.call_args
        assert call_kwargs.kwargs.get("expected_output") is None


# ============================================================
# evaluate_tool_correctness 测试
# ============================================================
class TestEvaluateToolCorrectness:
    """测试工具调用正确性评测"""

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_tool_correctness_basic(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试基本的工具正确性评测调用"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="tool_correctness",
            metric_name="工具正确性",
            score=1.0,
            success=True,
            reason="正确选择了工具",
        )
        mock_evaluate.return_value = expected_result

        tools_called = [{"name": "search", "input_parameters": {"query": "天气"}}]
        expected_tools = [{"name": "search", "input_parameters": {"query": "天气"}}]

        result = AgentEvaluator.evaluate_tool_correctness(
            input_text="请帮我搜索天气",
            tools_called=tools_called,
            expected_tools=expected_tools,
            threshold=0.8,
        )

        # 验证 create_test_case 调用
        mock_create_test_case.assert_called_once_with(
            input_text="请帮我搜索天气",
            actual_output="",
            tools_called=tools_called,
            expected_tools=expected_tools,
        )

        # 验证 evaluate 调用
        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="tool_correctness",
            threshold=0.8,
            metric_name="工具正确性",
        )

        assert result == expected_result
        assert result.score == 1.0

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_tool_correctness_default_threshold(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试工具正确性评测使用默认阈值"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="tool_correctness",
            metric_name="工具正确性",
            score=0.7,
            success=True,
        )

        AgentEvaluator.evaluate_tool_correctness(
            input_text="输入",
            tools_called=[{"name": "search"}],
            expected_tools=[{"name": "search"}],
        )

        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5 or call_kwargs[1].get("threshold") == 0.5

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_tool_correctness_wrong_tools(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试工具选择错误时的评测结果"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="tool_correctness",
            metric_name="工具正确性",
            score=0.0,
            success=False,
            reason="选择了错误的工具",
        )

        result = AgentEvaluator.evaluate_tool_correctness(
            input_text="请帮我搜索天气",
            tools_called=[{"name": "calculator"}],
            expected_tools=[{"name": "search"}],
        )

        assert result.success is False
        assert result.score == 0.0


# ============================================================
# evaluate_step_efficiency 测试
# ============================================================
class TestEvaluateStepEfficiency:
    """测试步骤效率评测"""

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_step_efficiency_basic(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试基本的步骤效率评测调用"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="step_efficiency",
            metric_name="步骤效率",
            score=0.85,
            success=True,
            reason="步骤执行高效",
        )
        mock_evaluate.return_value = expected_result

        result = AgentEvaluator.evaluate_step_efficiency(
            input_text="请帮我搜索北京天气",
            actual_output="北京今天晴，温度25度",
            expected_output="北京天气信息",
            threshold=0.6,
        )

        # 验证 create_test_case 调用
        mock_create_test_case.assert_called_once_with(
            input_text="请帮我搜索北京天气",
            actual_output="北京今天晴，温度25度",
            expected_output="北京天气信息",
        )

        # 验证 evaluate 调用
        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="step_efficiency",
            threshold=0.6,
            metric_name="步骤效率",
        )

        assert result == expected_result
        assert result.score == 0.85

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_step_efficiency_default_threshold(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试步骤效率评测使用默认阈值"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="step_efficiency",
            metric_name="步骤效率",
            score=0.5,
            success=True,
        )

        AgentEvaluator.evaluate_step_efficiency(
            input_text="输入",
            actual_output="输出",
        )

        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5 or call_kwargs[1].get("threshold") == 0.5

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_step_efficiency_inefficient(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试步骤效率低的评测结果"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="step_efficiency",
            metric_name="步骤效率",
            score=0.2,
            success=False,
            reason="步骤冗余，效率低下",
        )

        result = AgentEvaluator.evaluate_step_efficiency(
            input_text="输入",
            actual_output="输出",
        )

        assert result.success is False
        assert result.score == 0.2


# ============================================================
# evaluate_all 测试
# ============================================================
class TestEvaluateAll:
    """测试综合评测（并行执行所有指标）"""

    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_step_efficiency")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_tool_correctness")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_task_completion")
    def test_evaluate_all_returns_all_metrics(
        self,
        mock_task_completion,
        mock_tool_correctness,
        mock_step_efficiency,
    ):
        """测试综合评测返回所有指标结果"""
        from app.services.agent_evaluator import AgentEvaluator

        # 准备 mock 结果
        tc_result = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.9,
            success=True,
        )
        tool_result = TransformedMetricResult(
            metric_key="tool_correctness",
            metric_name="工具正确性",
            score=0.8,
            success=True,
        )
        step_result = TransformedMetricResult(
            metric_key="step_efficiency",
            metric_name="步骤效率",
            score=0.7,
            success=True,
        )

        mock_task_completion.return_value = tc_result
        mock_tool_correctness.return_value = tool_result
        mock_step_efficiency.return_value = step_result

        result = asyncio.run(
            AgentEvaluator.evaluate_all(
                task="搜索天气",
                input_text="请帮我搜索北京天气",
                actual_output="北京今天晴",
                expected_output="北京天气信息",
                tools_called=[{"name": "search"}],
                expected_tools=[{"name": "search"}],
            )
        )

        # 验证返回字典包含所有三个指标
        assert "task_completion" in result
        assert "tool_correctness" in result
        assert "step_efficiency" in result
        assert len(result) == 3

        # 验证各指标结果
        assert result["task_completion"].score == 0.9
        assert result["tool_correctness"].score == 0.8
        assert result["step_efficiency"].score == 0.7

    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_step_efficiency")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_tool_correctness")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_task_completion")
    def test_evaluate_all_passes_threshold(
        self,
        mock_task_completion,
        mock_tool_correctness,
        mock_step_efficiency,
    ):
        """测试综合评测传递阈值参数"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_task_completion.return_value = TransformedMetricResult(
            metric_key="task_completion", metric_name="任务完成度", score=0.5, success=True
        )
        mock_tool_correctness.return_value = TransformedMetricResult(
            metric_key="tool_correctness", metric_name="工具正确性", score=0.5, success=True
        )
        mock_step_efficiency.return_value = TransformedMetricResult(
            metric_key="step_efficiency", metric_name="步骤效率", score=0.5, success=True
        )

        thresholds = {
            "task_completion": 0.8,
            "tool_correctness": 0.7,
            "step_efficiency": 0.6,
        }

        asyncio.run(
            AgentEvaluator.evaluate_all(
                task="测试",
                input_text="输入",
                actual_output="输出",
                thresholds=thresholds,
            )
        )

        # 验证各评测方法接收到正确的 threshold
        tc_kwargs = mock_task_completion.call_args.kwargs
        assert tc_kwargs["threshold"] == 0.8

        tool_kwargs = mock_tool_correctness.call_args.kwargs
        assert tool_kwargs["threshold"] == 0.7

        step_kwargs = mock_step_efficiency.call_args.kwargs
        assert step_kwargs["threshold"] == 0.6

    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_step_efficiency")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_tool_correctness")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_task_completion")
    def test_evaluate_all_default_thresholds(
        self,
        mock_task_completion,
        mock_tool_correctness,
        mock_step_efficiency,
    ):
        """测试综合评测使用默认阈值"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_task_completion.return_value = TransformedMetricResult(
            metric_key="task_completion", metric_name="任务完成度", score=0.5, success=True
        )
        mock_tool_correctness.return_value = TransformedMetricResult(
            metric_key="tool_correctness", metric_name="工具正确性", score=0.5, success=True
        )
        mock_step_efficiency.return_value = TransformedMetricResult(
            metric_key="step_efficiency", metric_name="步骤效率", score=0.5, success=True
        )

        asyncio.run(
            AgentEvaluator.evaluate_all(
                task="测试",
                input_text="输入",
                actual_output="输出",
            )
        )

        # 验证默认 threshold=0.5
        tc_kwargs = mock_task_completion.call_args.kwargs
        assert tc_kwargs["threshold"] == 0.5

        tool_kwargs = mock_tool_correctness.call_args.kwargs
        assert tool_kwargs["threshold"] == 0.5

        step_kwargs = mock_step_efficiency.call_args.kwargs
        assert step_kwargs["threshold"] == 0.5

    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_step_efficiency")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_tool_correctness")
    @patch("app.services.agent_evaluator.AgentEvaluator.evaluate_task_completion")
    def test_evaluate_all_partial_failure(
        self,
        mock_task_completion,
        mock_tool_correctness,
        mock_step_efficiency,
    ):
        """测试综合评测中部分指标失败"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_task_completion.return_value = TransformedMetricResult(
            metric_key="task_completion", metric_name="任务完成度", score=0.9, success=True
        )
        mock_tool_correctness.side_effect = RuntimeError("工具评测失败")
        mock_step_efficiency.return_value = TransformedMetricResult(
            metric_key="step_efficiency", metric_name="步骤效率", score=0.7, success=True
        )

        result = asyncio.run(
            AgentEvaluator.evaluate_all(
                task="测试",
                input_text="输入",
                actual_output="输出",
            )
        )

        # 成功的指标应有结果
        assert result["task_completion"].success is True
        assert result["step_efficiency"].success is True

        # 失败的指标应有错误信息
        assert result["tool_correctness"].success is False
        assert result["tool_correctness"].error is not None


# ============================================================
# evaluate_task_completion_batch 测试
# ============================================================
class TestEvaluateTaskCompletionBatch:
    """测试批量评测任务完成度"""

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_multiple_tasks(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试批量评测多个任务"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.8,
            success=True,
        )

        tasks = [
            {
                "task": "搜索天气",
                "input_text": "搜索北京天气",
                "actual_output": "北京晴",
                "expected_output": "北京天气",
            },
            {
                "task": "查询航班",
                "input_text": "查询航班信息",
                "actual_output": "航班正常",
                "expected_output": "航班信息",
            },
            {
                "task": "预订酒店",
                "input_text": "预订北京酒店",
                "actual_output": "预订成功",
                "expected_output": "预订确认",
            },
        ]

        results = asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(
                tasks=tasks,
                threshold=0.7,
                max_concurrent=2,
            )
        )

        assert len(results) == 3
        assert all(isinstance(r, TransformedMetricResult) for r in results)
        assert mock_evaluate.call_count == 3

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_empty_tasks(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试空任务列表返回空结果"""
        from app.services.agent_evaluator import AgentEvaluator

        results = asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(
                tasks=[],
                threshold=0.5,
            )
        )

        assert results == []
        mock_evaluate.assert_not_called()

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_default_threshold(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试批量评测使用默认阈值"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.5,
            success=True,
        )

        tasks = [
            {
                "task": "测试",
                "input_text": "输入",
                "actual_output": "输出",
            },
        ]

        asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(tasks=tasks)
        )

        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5 or call_kwargs[1].get("threshold") == 0.5

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_concurrency_control(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试并发控制 - max_concurrent 限制并发数"""
        import asyncio
        from app.services.agent_evaluator import AgentEvaluator

        concurrent_count = {"current": 0, "max": 0}

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            concurrent_count["current"] += 1
            concurrent_count["max"] = max(
                concurrent_count["max"], concurrent_count["current"]
            )
            # 模拟异步延迟
            import time
            time.sleep(0.01)
            concurrent_count["current"] -= 1
            return TransformedMetricResult(
                metric_key=metric_key,
                metric_name=metric_key,
                score=0.8,
                success=True,
            )

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.side_effect = side_effect_func

        tasks = [
            {
                "task": f"任务{i}",
                "input_text": f"输入{i}",
                "actual_output": f"输出{i}",
            }
            for i in range(10)
        ]

        results = asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(
                tasks=tasks,
                threshold=0.5,
                max_concurrent=3,
            )
        )

        assert len(results) == 10
        assert concurrent_count["max"] <= 3

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_partial_failure(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试批量评测中部分任务失败"""
        from app.services.agent_evaluator import AgentEvaluator

        call_count = {"n": 0}

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("评测失败")
            return TransformedMetricResult(
                metric_key=metric_key,
                metric_name="任务完成度",
                score=0.8,
                success=True,
            )

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.side_effect = side_effect_func

        tasks = [
            {"task": f"任务{i}", "input_text": f"输入{i}", "actual_output": f"输出{i}"}
            for i in range(3)
        ]

        results = asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(
                tasks=tasks,
                threshold=0.5,
                max_concurrent=1,
            )
        )

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].error is not None
        assert results[2].success is True

    @patch("app.services.agent_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.agent_evaluator.deepeval_adapter.create_test_case")
    def test_batch_evaluate_with_optional_fields(
        self, mock_create_test_case, mock_evaluate
    ):
        """测试批量评测支持任务中的可选字段"""
        from app.services.agent_evaluator import AgentEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="task_completion",
            metric_name="任务完成度",
            score=0.8,
            success=True,
        )

        tasks = [
            {
                "task": "搜索天气",
                "input_text": "搜索北京天气",
                "actual_output": "北京晴",
                # 没有 expected_output
            },
        ]

        asyncio.run(
            AgentEvaluator.evaluate_task_completion_batch(tasks=tasks)
        )

        # 验证 create_test_case 调用时 expected_output 为 None
        call_kwargs = mock_create_test_case.call_args
        assert call_kwargs.kwargs.get("expected_output") is None
