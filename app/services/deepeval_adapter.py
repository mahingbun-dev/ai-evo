"""
DeepEval 适配器核心

封装 DeepEval 指标，为 AI-EVO 提供统一的评测接口。

功能：
- create_test_case: 创建 DeepEval 测试用例
- evaluate: 执行单个指标评测
- evaluate_batch: 批量执行指标评测（支持并发控制）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from deepeval.test_case import LLMTestCase, ToolCall

from app.services.deepeval_metric_registry import (
    DeepEvalMetricRegistry,
    MetricNotFoundError,
)
from app.services.deepeval_result_transformer import (
    DeepEvalResultTransformer,
    TransformedMetricResult,
)

logger = logging.getLogger(__name__)


def _convert_to_tool_call(tool_data: Dict[str, Any]) -> ToolCall:
    """
    将字典格式的工具数据转换为 DeepEval ToolCall 对象。

    Args:
        tool_data: 包含工具信息的字典，支持以下字段：
            - name (str, 必需): 工具名称
            - description (str, 可选): 工具描述
            - reasoning (str, 可选): 调用原因
            - input_parameters (dict, 可选): 输入参数
            - output (Any, 可选): 工具输出

    Returns:
        ToolCall: DeepEval 的 ToolCall 对象
    """
    return ToolCall(
        name=tool_data["name"],
        description=tool_data.get("description"),
        reasoning=tool_data.get("reasoning"),
        input_parameters=tool_data.get("input_parameters"),
        output=tool_data.get("output"),
    )


def _convert_tool_calls(
    tools: Optional[List[Dict[str, Any]]],
) -> Optional[List[ToolCall]]:
    """
    将工具字典列表转换为 ToolCall 列表。

    Args:
        tools: 工具字典列表，或 None

    Returns:
        ToolCall 列表，或 None（当输入为 None 时）
    """
    if tools is None:
        return None
    return [_convert_to_tool_call(t) for t in tools]


def create_test_case(
    input_text: str,
    actual_output: str,
    expected_output: Optional[str] = None,
    context: Optional[List[str]] = None,
    retrieval_context: Optional[List[str]] = None,
    tools_called: Optional[List[Dict[str, Any]]] = None,
    expected_tools: Optional[List[Dict[str, Any]]] = None,
) -> LLMTestCase:
    """
    创建 DeepEval 测试用例。

    将 AI-EVO 的数据格式转换为 DeepEval 的 LLMTestCase。

    Args:
        input_text: 输入文本（用户的查询/问题）
        actual_output: 模型的实际输出
        expected_output: 期望输出（可选）
        context: 上下文列表（可选）
        retrieval_context: 检索上下文列表（可选）
        tools_called: 实际调用的工具列表（可选），每个工具为字典格式：
            {"name": "...", "description": "...", "input_parameters": {...}, ...}
        expected_tools: 期望调用的工具列表（可选），格式同 tools_called

    Returns:
        LLMTestCase: DeepEval 测试用例对象
    """
    return LLMTestCase(
        input=input_text,
        actual_output=actual_output,
        expected_output=expected_output,
        context=context,
        retrieval_context=retrieval_context,
        tools_called=_convert_tool_calls(tools_called),
        expected_tools=_convert_tool_calls(expected_tools),
    )


def evaluate(
    test_case: LLMTestCase,
    metric_key: str,
    threshold: float = 0.5,
    metric_name: Optional[str] = None,
    **kwargs: Any,
) -> TransformedMetricResult:
    """
    执行单个指标评测。

    流程：
    1. 通过 DeepEvalMetricRegistry 创建指标实例
    2. 调用指标的 measure 方法执行评测
    3. 使用 DeepEvalResultTransformer 转换结果

    Args:
        test_case: DeepEval 测试用例
        metric_key: 指标标识符（如 'answer_relevancy', 'faithfulness'）
        threshold: 指标阈值，默认 0.5
        metric_name: 指标显示名称，默认使用 metric_key
        **kwargs: 传递给指标构造函数的额外参数

    Returns:
        TransformedMetricResult: 转换后的指标评测结果
    """
    if metric_name is None:
        metric_name = metric_key

    try:
        # 1. 创建指标实例
        metric = DeepEvalMetricRegistry.create_metric(
            metric_key, threshold=threshold, **kwargs
        )

        # 2. 执行评测
        metric.measure(test_case)

        # 3. 转换结果
        return DeepEvalResultTransformer.transform_metric_result(
            metric_result=metric,
            metric_key=metric_key,
            metric_name=metric_name,
        )

    except MetricNotFoundError as e:
        logger.error("指标不存在: %s", e)
        return TransformedMetricResult(
            metric_key=metric_key,
            metric_name=metric_name,
            score=0.0,
            success=False,
            error=str(e),
        )

    except Exception as e:
        logger.error("评测执行失败 [%s]: %s", metric_key, e, exc_info=True)
        return TransformedMetricResult(
            metric_key=metric_key,
            metric_name=metric_name,
            score=0.0,
            success=False,
            error=str(e),
        )


async def evaluate_batch(
    test_cases: List[LLMTestCase],
    metric_configs: List[Dict[str, Any]],
    max_concurrent: int = 5,
) -> List[TransformedMetricResult]:
    """
    批量执行指标评测。

    为每个 test_case × metric_config 组合创建评测任务，
    使用 asyncio.Semaphore 限制并发数。

    Args:
        test_cases: DeepEval 测试用例列表
        metric_configs: 指标配置列表，每个配置包含：
            - key (str): 指标标识符
            - threshold (float, 可选): 阈值，默认 0.5
            - name (str, 可选): 指标显示名称
            - **kwargs: 其他传递给指标的参数
        max_concurrent: 最大并发数，默认 5

    Returns:
        List[TransformedMetricResult]: 所有评测结果列表，
        顺序为 test_cases[0] × metrics[0], test_cases[0] × metrics[1], ...,
        test_cases[1] × metrics[0], ...
    """
    if not test_cases or not metric_configs:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _run_single(
        test_case: LLMTestCase,
        config: Dict[str, Any],
    ) -> TransformedMetricResult:
        """在信号量控制下执行单个评测任务"""
        async with semaphore:
            # evaluate 是同步函数，在事件循环中用 run_in_executor 执行
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: evaluate(
                    test_case=test_case,
                    metric_key=config["key"],
                    threshold=config.get("threshold", 0.5),
                    metric_name=config.get("name"),
                    **{
                        k: v
                        for k, v in config.items()
                        if k not in ("key", "threshold", "name")
                    },
                ),
            )

    # 构建所有评测任务
    tasks = []
    for test_case in test_cases:
        for config in metric_configs:
            tasks.append(_run_single(test_case, config))

    # 并发执行所有任务
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 处理异常结果：将未捕获的异常转为错误结果
    final_results: List[TransformedMetricResult] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # 计算对应的 test_case 和 metric_config 索引
            config_idx = i % len(metric_configs)
            config = metric_configs[config_idx]
            logger.error(
                "批量评测任务 %d 异常 [%s]: %s", i, config["key"], result
            )
            final_results.append(
                TransformedMetricResult(
                    metric_key=config["key"],
                    metric_name=config.get("name", config["key"]),
                    score=0.0,
                    success=False,
                    error=str(result),
                )
            )
        else:
            final_results.append(result)

    return final_results
