"""
成功率评测服务
"""
import asyncio
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field

from app.services.model_caller import ModelCaller


@dataclass
class SuccessRateResult:
    """成功率评测结果"""
    question_index: int
    repeat_results: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    error_analysis: Dict[str, Any] = field(default_factory=dict)


class SuccessRateEvaluator:
    """
    成功率评测服务

    对同一评测数据进行多次重复调用，统计生成成功的比率
    不依赖裁判模型，直接调用被评测模型
    """

    def __init__(
        self,
        model_config: Dict[str, Any],
        success_criteria: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 30,
        progress_callback: Optional[Callable] = None,
    ):
        """
        初始化成功率评测服务

        Args:
            model_config: 被评测模型配置
            success_criteria: 成功判定标准
            timeout_seconds: 单次调用超时时间
            progress_callback: 进度回调函数
        """
        self.model_config = model_config
        self.success_criteria = success_criteria or {
            "non_empty": True,
            "no_error": True,
            "valid_format": True,
        }
        self.timeout_seconds = timeout_seconds
        self.progress_callback = progress_callback

    async def _report_progress(self, current: int, total: int, message: str):
        """报告进度"""
        if self.progress_callback:
            try:
                await self.progress_callback(current, total, message)
            except Exception:
                pass

    def _check_success(
        self,
        result: Dict[str, Any],
    ) -> tuple[bool, str]:
        """
        检查是否成功

        Args:
            result: 调用结果

        Returns:
            (是否成功, 失败原因)
        """
        content = result.get("content", "")
        error = result.get("error", "")

        # 检查是否有错误
        if self.success_criteria.get("no_error", True) and error:
            return False, error

        # 检查是否非空
        if self.success_criteria.get("non_empty", True) and not content:
            return False, "返回内容为空"

        # 检查格式是否有效（如果是JSON）
        if self.success_criteria.get("valid_format", True):
            import json
            try:
                if content.strip().startswith('{') or content.strip().startswith('['):
                    json.loads(content)
            except json.JSONDecodeError:
                # 允许非JSON格式，只记录
                pass

        return True, ""

    async def evaluate_single(
        self,
        question: str,
        repeat_count: int = 10,
    ) -> SuccessRateResult:
        """
        评测单条数据的成功率

        Args:
            question: 用户问题
            repeat_count: 重复次数

        Returns:
            成功率评测结果
        """
        await self._report_progress(0, repeat_count, f"开始成功率评测，重复{repeat_count}次")

        repeat_results = []
        response_times = []
        errors = []

        # 多次调用模型
        for i in range(repeat_count):
            await self._report_progress(i + 1, repeat_count, f"第 {i + 1}/{repeat_count} 次调用")

            start_time = asyncio.get_event_loop().time()

            try:
                messages = [{"role": "user", "content": question}]
                result = await ModelCaller.call(messages, self.model_config)

                response_time = asyncio.get_event_loop().time() - start_time
                response_times.append(response_time)

                success, error_msg = self._check_success(result)

                repeat_results.append({
                    "repeat_id": i + 1,
                    "success": success,
                    "response_time": round(response_time, 2),
                    "error": error_msg if not success else None,
                })

                if not success:
                    errors.append({
                        "attempt": i + 1,
                        "reason": error_msg,
                    })

            except Exception as e:
                response_time = asyncio.get_event_loop().time() - start_time
                response_times.append(response_time)

                repeat_results.append({
                    "repeat_id": i + 1,
                    "success": False,
                    "response_time": round(response_time, 2),
                    "error": str(e),
                })
                errors.append({
                    "attempt": i + 1,
                    "reason": str(e),
                })

            # 短暂延迟，避免限流
            await asyncio.sleep(0.5)

        # 计算统计指标
        successful_calls = sum(1 for r in repeat_results if r["success"])
        total_calls = repeat_count
        success_rate = successful_calls / total_calls if total_calls > 0 else 0.0

        # pass^k 指标（M2 QE2-3，语义对齐 τ-bench）：repeat_count 次 trial
        # 的通过序列 → pass@1 / pass^k 与分数标准差/标准误（0/1 二值分数）。
        from app.services.pass_k import score_stats, trial_pass_stats

        passes = [bool(r.get("success")) for r in repeat_results]
        pass_k_metrics = trial_pass_stats(passes)
        pass_k_metrics.update(score_stats([1.0 if p else 0.0 for p in passes]))

        metrics = {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "success_rate": round(success_rate, 4),
            "avg_response_time": round(sum(response_times) / len(response_times), 2) if response_times else 0,
            "min_response_time": round(min(response_times), 2) if response_times else 0,
            "max_response_time": round(max(response_times), 2) if response_times else 0,
        }
        metrics.update(pass_k_metrics)

        # 错误分析
        error_analysis = {}
        error_types = {}
        for err in errors:
            reason = err.get("reason", "unknown")
            error_types[reason] = error_types.get(reason, 0) + 1

        error_analysis = {
            "timeout": sum(1 for err in errors if "timeout" in err.get("reason", "").lower()),
            "rate_limit": sum(1 for err in errors if "rate" in err.get("reason", "").lower()),
            "invalid_response": sum(1 for err in errors if "invalid" in err.get("reason", "").lower()),
            "other": sum(1 for err in errors if not any(k in err.get("reason", "").lower() for k in ["timeout", "rate", "invalid"])),
            "error_distribution": error_types,
        }

        return SuccessRateResult(
            question_index=0,
            repeat_results=repeat_results,
            metrics=metrics,
            error_analysis=error_analysis,
        )

    async def evaluate_batch(
        self,
        questions: List[str],
        repeat_count: int = 10,
    ) -> List[SuccessRateResult]:
        """
        批量成功率评测

        Args:
            questions: 问题列表
            repeat_count: 重复次数

        Returns:
            成功率评测结果列表
        """
        results = []

        for idx, question in enumerate(questions):
            await self._report_progress(idx + 1, len(questions), f"评测第 {idx + 1}/{len(questions)} 条")

            result = await self.evaluate_single(question, repeat_count)
            result.question_index = idx
            results.append(result)

        return results
