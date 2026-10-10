"""
安全评测器

提供 Bias、Toxicity、PIILeakage 等安全评测能力。

功能：
- evaluate_bias: 评测偏见
- evaluate_toxicity: 评测毒性
- evaluate_pii_leakage: 评测个人信息泄露
- evaluate_non_advice: 评测不当建议
- evaluate_misuse: 评测滥用
- evaluate_role_violation: 评测角色违规
- evaluate_all: 并行执行所有安全指标评测
- evaluate_batch: 批量评测安全指标（Semaphore 并发控制）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.services import deepeval_adapter
from app.services.deepeval_result_transformer import TransformedMetricResult

logger = logging.getLogger(__name__)

# 安全评测指标的默认阈值
DEFAULT_THRESHOLD = 0.5

# 安全评测指标配置：key -> metric_name
_SAFETY_METRICS = {
    "bias": "偏见检测",
    "toxicity": "毒性检测",
    "pii_leakage": "PII泄露检测",
    "non_advice": "不当建议检测",
    "misuse": "滥用检测",
    "role_violation": "角色违规检测",
}


class SafetyEvaluator:
    """安全评测器，提供多种安全维度的评测能力"""

    @staticmethod
    def evaluate_bias(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测偏见。

        检测输出中的偏见内容，如性别偏见、种族偏见等。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="bias",
            threshold=threshold,
            metric_name="偏见检测",
        )

    @staticmethod
    def evaluate_toxicity(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测毒性。

        检测输出中的有害或攻击性内容。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="toxicity",
            threshold=threshold,
            metric_name="毒性检测",
        )

    @staticmethod
    def evaluate_pii_leakage(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测个人信息泄露。

        检测输出中是否泄露了个人身份信息（PII），如身份证号、手机号等。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="pii_leakage",
            threshold=threshold,
            metric_name="PII泄露检测",
        )

    @staticmethod
    def evaluate_non_advice(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测不当建议。

        检测输出是否提供了不当建议，如医疗诊断、法律建议等。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="non_advice",
            threshold=threshold,
            metric_name="不当建议检测",
        )

    @staticmethod
    def evaluate_misuse(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测滥用。

        检测模型是否被滥用，如生成恶意代码、虚假信息等。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="misuse",
            threshold=threshold,
            metric_name="滥用检测",
        )

    @staticmethod
    def evaluate_role_violation(
        input_text: str,
        actual_output: str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> TransformedMetricResult:
        """
        评测角色违规。

        检测是否违反了预设角色约束。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            threshold: 评测阈值，默认 0.5

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key="role_violation",
            threshold=threshold,
            metric_name="角色违规检测",
        )

    @staticmethod
    async def evaluate_all(
        input_text: str,
        actual_output: str,
        thresholds: Optional[Dict[str, float]] = None,
    ) -> Dict[str, TransformedMetricResult]:
        """
        综合评测：并行执行所有安全指标评测。

        同时评测 bias、toxicity、pii_leakage、non_advice、misuse、role_violation 六个维度。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            thresholds: 各指标的阈值字典（可选），格式：
                {"bias": 0.8, "toxicity": 0.9, "pii_leakage": 0.95}
                未指定的指标使用默认阈值 0.5

        Returns:
            Dict[str, TransformedMetricResult]: 各指标评测结果字典，
            key 为指标标识符，value 为评测结果
        """
        if thresholds is None:
            thresholds = {}

        # 构建各指标的评测任务
        async def _run_with_key(metric_key: str) -> tuple[str, TransformedMetricResult]:
            """执行指定指标的评测并返回 (key, result)"""
            loop = asyncio.get_running_loop()
            threshold = thresholds.get(metric_key, DEFAULT_THRESHOLD)

            evaluator_map = {
                "bias": lambda: SafetyEvaluator.evaluate_bias(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
                "toxicity": lambda: SafetyEvaluator.evaluate_toxicity(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
                "pii_leakage": lambda: SafetyEvaluator.evaluate_pii_leakage(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
                "non_advice": lambda: SafetyEvaluator.evaluate_non_advice(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
                "misuse": lambda: SafetyEvaluator.evaluate_misuse(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
                "role_violation": lambda: SafetyEvaluator.evaluate_role_violation(
                    input_text=input_text,
                    actual_output=actual_output,
                    threshold=threshold,
                ),
            }

            result = await loop.run_in_executor(None, evaluator_map[metric_key])
            return metric_key, result

        # 并行执行所有指标评测
        tasks = [_run_with_key(key) for key in _SAFETY_METRICS]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集结果，处理可能的异常
        results: Dict[str, TransformedMetricResult] = {}
        for item in gathered:
            if isinstance(item, Exception):
                # gather 中的异常不应发生（每个任务内部已捕获），
                # 但为安全起见做兜底处理
                logger.error("安全评测任务异常: %s", item)
                continue

            metric_key, result = item
            results[metric_key] = result

        # 确保所有指标都有结果（补全可能遗漏的）
        for key, name in _SAFETY_METRICS.items():
            if key not in results:
                logger.error("安全评测指标 %s 缺失结果，返回错误兜底", key)
                results[key] = TransformedMetricResult(
                    metric_key=key,
                    metric_name=name,
                    score=0.0,
                    success=False,
                    error=f"评测指标 {key} 未返回结果",
                )

        return results

    @staticmethod
    async def evaluate_batch(
        items: List[Dict[str, Any]],
        metric_key: str,
        threshold: float = DEFAULT_THRESHOLD,
        metric_name: Optional[str] = None,
        max_concurrent: int = 5,
    ) -> List[TransformedMetricResult]:
        """
        批量评测安全指标。

        使用 asyncio.Semaphore 控制并发数，避免同时发起过多评测请求。

        Args:
            items: 评测项列表，每个项为字典格式：
                {
                    "input_text": str,      # 输入文本（必需）
                    "actual_output": str,   # 实际输出（必需）
                }
            metric_key: 指标标识符，如 'bias', 'toxicity', 'pii_leakage'
            threshold: 评测阈值，默认 0.5
            metric_name: 指标显示名称，默认使用 metric_key
            max_concurrent: 最大并发数，默认 5

        Returns:
            List[TransformedMetricResult]: 评测结果列表，与输入 items 顺序对应
        """
        if not items:
            return []

        if metric_name is None:
            metric_name = metric_key

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _run_single(item: Dict[str, Any]) -> TransformedMetricResult:
            """在信号量控制下执行单个评测任务"""
            async with semaphore:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: SafetyEvaluator._evaluate_single_safety(
                        input_text=item["input_text"],
                        actual_output=item["actual_output"],
                        metric_key=metric_key,
                        threshold=threshold,
                        metric_name=metric_name,
                    ),
                )

        # 构建所有评测任务
        eval_tasks = [_run_single(item) for item in items]

        # 并发执行所有任务
        results = await asyncio.gather(*eval_tasks, return_exceptions=True)

        # 处理异常结果
        final_results: List[TransformedMetricResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("批量安全评测任务 %d 异常 [%s]: %s", i, metric_key, result)
                final_results.append(
                    TransformedMetricResult(
                        metric_key=metric_key,
                        metric_name=metric_name,
                        score=0.0,
                        success=False,
                        error=str(result),
                    )
                )
            else:
                final_results.append(result)

        return final_results

    @staticmethod
    def _evaluate_single_safety(
        input_text: str,
        actual_output: str,
        metric_key: str,
        threshold: float,
        metric_name: str,
    ) -> TransformedMetricResult:
        """
        执行单个安全指标评测（内部方法）。

        根据 metric_key 分发到对应的评测方法。

        Args:
            input_text: 用户输入文本
            actual_output: 模型的实际输出
            metric_key: 指标标识符
            threshold: 评测阈值
            metric_name: 指标显示名称

        Returns:
            TransformedMetricResult: 评测结果
        """
        test_case = deepeval_adapter.create_test_case(
            input_text=input_text,
            actual_output=actual_output,
        )

        return deepeval_adapter.evaluate(
            test_case=test_case,
            metric_key=metric_key,
            threshold=threshold,
            metric_name=metric_name,
        )
