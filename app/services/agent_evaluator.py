"""
Agent 评测器

提供 TaskCompletion、ToolCorrectness、StepEfficiency 等 Agent 评测能力。

功能：
- evaluate_task_completion: 评测任务完成度
- evaluate_tool_correctness: 评测工具调用正确性
- evaluate_step_efficiency: 评测步骤效率
- evaluate_all: 并行执行所有 Agent 指标评测
- evaluate_task_completion_batch: 批量评测任务完成度（Semaphore 并发控制）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.services import deepeval_adapter
from app.services.deepeval_result_transformer import TransformedMetricResult

logger = logging.getLogger(__name__)

# Agent 评测指标的默认阈值
DEFAULT_THRESHOLD = 0.5


class AgentEvaluator:
    """Agent 评测器，提供多种 Agent 维度的评测能力"""

    @staticmethod
    def evaluate_task_completion(
        task: str,
        input_text: str,
        actual_output: str,
        expected_output: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测任务完成度。

        评估 Agent 是否成功完成了指定任务。

        Args:
            task: 任务描述
            input_text: 用户输入文本
            actual_output: Agent 的实际输出
            expected_output: 期望输出（可选）
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
            expected_output=expected_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="task_completion",
            threshold=threshold,
            metric_name="任务完成度",
        )

    @staticmethod
    def evaluate_tool_correctness(
        input_text: str,
        tools_called: List[Dict[str, Any]],
        expected_tools: List[Dict[str, Any]],
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测工具调用正确性。

        评估 Agent 是否选择了正确的工具。

        Args:
            input_text: 用户输入文本
            tools_called: 实际调用的工具列表
            expected_tools: 期望调用的工具列表
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output="",
            tools_called=tools_called,
            expected_tools=expected_tools,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="tool_correctness",
            threshold=threshold,
            metric_name="工具正确性",
        )

    @staticmethod
    def evaluate_step_efficiency(
        input_text: str,
        actual_output: str,
        expected_output: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测步骤效率。

        评估 Agent 完成任务的步骤是否高效。

        Args:
            input_text: 用户输入文本
            actual_output: Agent 的实际输出
            expected_output: 期望输出（可选）
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
            expected_output=expected_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="step_efficiency",
            threshold=threshold,
            metric_name="步骤效率",
        )

    @staticmethod
    async def evaluate_all(
        task: str,
        input_text: str,
        actual_output: str,
        expected_output: Optional[str] = None,
        tools_called: Optional[List[Dict[str, Any]]] = None,
        expected_tools: Optional[List[Dict[str, Any]]] = None,
        thresholds: Optional[Dict[str, float]] = None,
    ) -> Dict[str, TransformedMetricResult]:
        """
        综合评测：并行执行所有 Agent 指标评测。

        同时评测 task_completion、tool_correctness、step_efficiency 三个维度。

        Args:
            task: 任务描述
            input_text: 用户输入文本
            actual_output: Agent 的实际输出
            expected_output: 期望输出（可选）
            tools_called: 实际调用的工具列表（可选，用于工具正确性评测）
            expected_tools: 期望调用的工具列表（可选，用于工具正确性评测）
            thresholds: 各指标的阈值字典（可选），格式：
                {"task_completion": 0.8, "tool_correctness": 0.7, "step_efficiency": 0.6}
                未指定的指标使用默认阈值 0.5

        Returns:
            Dict[str, TransformedMetricResult]: 各指标评测结果字典，
            key 为指标标识符，value 为评测结果
        """
        if thresholds is None:
            thresholds = {}

        tc_threshold = thresholds.get("task_completion", DEFAULT_THRESHOLD)
        tool_threshold = thresholds.get("tool_correctness", DEFAULT_THRESHOLD)
        step_threshold = thresholds.get("step_efficiency", DEFAULT_THRESHOLD)

        # 并行执行三个指标评测
        loop = asyncio.get_running_loop()

        tc_task = loop.run_in_executor(
            None,
            lambda: AgentEvaluator.evaluate_task_completion(
                task=task,
                input_text=input_text,
                actual_output=actual_output,
                expected_output=expected_output,
                threshold=tc_threshold,
            ),
        )

        tool_task = loop.run_in_executor(
            None,
            lambda: AgentEvaluator.evaluate_tool_correctness(
                input_text=input_text,
                tools_called=tools_called or [],
                expected_tools=expected_tools or [],
                threshold=tool_threshold,
            ),
        )

        step_task = loop.run_in_executor(
            None,
            lambda: AgentEvaluator.evaluate_step_efficiency(
                input_text=input_text,
                actual_output=actual_output,
                expected_output=expected_output,
                threshold=step_threshold,
            ),
        )

        # 收集结果，处理可能的异常
        results: Dict[str, TransformedMetricResult] = {}

        tc_result, tool_result, step_result = await asyncio.gather(
            tc_task, tool_task, step_task, return_exceptions=True
        )

        # 处理 task_completion 结果
        if isinstance(tc_result, Exception):
            logger.error("任务完成度评测失败: %s", tc_result)
            # W16：适配失败标 metric_unavailable，不再伪装 score=0
            results["task_completion"] = TransformedMetricResult(
                metric_key="task_completion",
                metric_name="任务完成度",
                score=None,
                success=False,
                metric_unavailable=True,
                error=str(tc_result),
            )
        else:
            results["task_completion"] = tc_result

        # 处理 tool_correctness 结果
        if isinstance(tool_result, Exception):
            logger.error("工具正确性评测失败: %s", tool_result)
            results["tool_correctness"] = TransformedMetricResult(
                metric_key="tool_correctness",
                metric_name="工具正确性",
                score=None,
                success=False,
                metric_unavailable=True,
                error=str(tool_result),
            )
        else:
            results["tool_correctness"] = tool_result

        # 处理 step_efficiency 结果
        if isinstance(step_result, Exception):
            logger.error("步骤效率评测失败: %s", step_result)
            results["step_efficiency"] = TransformedMetricResult(
                metric_key="step_efficiency",
                metric_name="步骤效率",
                score=None,
                success=False,
                metric_unavailable=True,
                error=str(step_result),
            )
        else:
            results["step_efficiency"] = step_result

        return results

    @staticmethod
    async def evaluate_task_completion_batch(
        tasks: List[Dict[str, Any]],
        threshold: float = DEFAULT_THRESHOLD,
        max_concurrent: int = 5,
    ) -> List[TransformedMetricResult]:
        """
        批量评测任务完成度。

        使用 asyncio.Semaphore 控制并发数，避免同时发起过多评测请求。

        Args:
            tasks: 任务列表，每个任务为字典格式：
                {
                    "task": str,            # 任务描述（必需）
                    "input_text": str,      # 输入文本（必需）
                    "actual_output": str,   # 实际输出（必需）
                    "expected_output": str, # 期望输出（可选）
                }
            threshold: 评测阈值，默认 0.5
            max_concurrent: 最大并发数，默认 5

        Returns:
            List[TransformedMetricResult]: 评测结果列表，与输入 tasks 顺序对应
        """
        if not tasks:
            return []

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _run_single(task_data: Dict[str, Any]) -> TransformedMetricResult:
            """在信号量控制下执行单个评测任务"""
            async with semaphore:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: AgentEvaluator.evaluate_task_completion(
                        task=task_data["task"],
                        input_text=task_data["input_text"],
                        actual_output=task_data["actual_output"],
                        expected_output=task_data.get("expected_output"),
                        threshold=threshold,
                    ),
                )

        # 构建所有评测任务
        eval_tasks = [_run_single(task_data) for task_data in tasks]

        # 并发执行所有任务
        results = await asyncio.gather(*eval_tasks, return_exceptions=True)

        # 处理异常结果
        final_results: List[TransformedMetricResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("批量任务完成度评测任务 %d 异常: %s", i, result)
                # W16：适配失败标 metric_unavailable，不再伪装 score=0
                final_results.append(
                    TransformedMetricResult(
                        metric_key="task_completion",
                        metric_name="任务完成度",
                        score=None,
                        success=False,
                        metric_unavailable=True,
                        error=str(result),
                    )
                )
            else:
                final_results.append(result)

        return final_results
