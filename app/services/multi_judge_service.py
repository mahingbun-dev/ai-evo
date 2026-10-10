"""
多裁判评测服务
"""
import asyncio
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field

from app.services.judge_evaluation_engine import JudgeEvaluationEngine


@dataclass
class MultiJudgeResult:
    """多裁判评测结果"""
    question_index: int
    judge_results: List[Dict[str, Any]] = field(default_factory=list)
    aggregated: Dict[str, Any] = field(default_factory=dict)
    reasoning_analysis: Dict[str, Any] = field(default_factory=dict)


class MultiJudgeService:
    """
    多裁判评测服务

    使用多个裁判模型分别评分，然后聚合结果
    """

    def __init__(
        self,
        model_config: Dict[str, Any],
        judge_configs: List[Dict[str, Any]],
        aggregation_method: str = "average",
        enable_reasoning_analysis: bool = True,
        progress_callback: Optional[Callable] = None,
    ):
        """
        初始化多裁判服务

        Args:
            model_config: 被评测模型配置
            judge_configs: 裁判模型配置列表
            aggregation_method: 聚合方式 (average/weighted/consensus)
            enable_reasoning_analysis: 是否启用评分理由分析
            progress_callback: 进度回调函数
        """
        self.model_config = model_config
        self.judge_configs = judge_configs
        self.aggregation_method = aggregation_method
        self.enable_reasoning_analysis = enable_reasoning_analysis
        self.progress_callback = progress_callback

    async def _report_progress(self, current: int, total: int, message: str):
        """报告进度"""
        if self.progress_callback:
            try:
                await self.progress_callback(current, total, message)
            except Exception:
                pass

    async def _call_judge(
        self,
        judge_config: Dict[str, Any],
        question: str,
        ref_answer: str,
        model_answer: str,
        judge_id: str,
    ) -> Dict[str, Any]:
        """调用单个裁判"""
        engine = JudgeEvaluationEngine(
            model_config=self.model_config,
            judge_config=judge_config,
        )

        result = await engine.evaluate_single(question, ref_answer, model_answer)

        return {
            "judge_id": judge_id,
            "scores": result.scores,
            "overall_score": result.overall_score,
            "reasoning": result.reasoning,
            "success": result.success,
            "error": result.error,
        }

    def _aggregate_scores(
        self,
        judge_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        聚合评分

        Args:
            judge_results: 裁判评分结果列表

        Returns:
            聚合结果
        """
        if not judge_results:
            return {"average_score": 0.0}

        # 计算每个维度的平均分
        all_scores = {}
        for jr in judge_results:
            scores = jr.get("scores", {})
            if not isinstance(scores, dict):
                continue

            for key, value in scores.items():
                if isinstance(value, dict) and "score" in value:
                    if key not in all_scores:
                        all_scores[key] = []
                    all_scores[key].append(value["score"])

        # 计算平均分
        score_details = {}
        for key, scores in all_scores.items():
            if scores:
                score_details[key] = {
                    "average": round(sum(scores) / len(scores), 2),
                    "variance": round(sum((s - sum(scores) / len(scores)) ** 2 for s in scores) / len(scores), 2),
                    "min": min(scores),
                    "max": max(scores),
                }

        # 计算综合评分
        if "综合评分" in all_scores and all_scores["综合评分"]:
            overall_scores = all_scores["综合评分"]
            avg_overall = sum(overall_scores) / len(overall_scores)
        else:
            # 加权平均所有维度
            total = 0
            count = 0
            for key, scores in all_scores.items():
                if key != "综合评分" and scores:
                    total += sum(scores)
                    count += len(scores)
            avg_overall = total / count if count > 0 else 0

        return {
            "average_score": round(avg_overall, 2),
            "score_details": score_details,
            "aggregation_method": self.aggregation_method,
        }

    def _analyze_reasoning(
        self,
        judge_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        分析评分理由

        Args:
            judge_results: 裁判评分结果列表

        Returns:
            理由分析结果
        """
        common_points = []
        divergent_points = []

        # 简单分析：提取各裁判的理由
        reasonings = []
        for jr in judge_results:
            if jr.get("reasoning"):
                reasonings.append(jr["reasoning"])

        if not reasonings:
            return {
                "common_points": [],
                "divergent_points": [],
                "overall_summary": "无法分析评分理由",
                "recommendation": "请检查评分结果",
            }

        # 简化版分析
        overall_summary = f"共有 {len(reasonings)} 位裁判参与了评分。"
        recommendation = "建议根据各维度评分进行针对性改进。"

        return {
            "common_points": common_points,
            "divergent_points": divergent_points,
            "overall_summary": overall_summary,
            "recommendation": recommendation,
        }

    async def evaluate_single(
        self,
        question: str,
        ref_answer: str,
        model_answer: str,
    ) -> MultiJudgeResult:
        """
        使用多个裁判评测单条数据

        Args:
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            多裁判评测结果
        """
        judge_results = []

        # 并行调用所有裁判
        tasks = []
        for idx, judge_config in enumerate(self.judge_configs):
            judge_id = f"judge_{idx + 1}"
            task = self._call_judge(judge_config, question, ref_answer, model_answer, judge_id)
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                judge_results.append({
                    "judge_id": "unknown",
                    "scores": {},
                    "overall_score": 0,
                    "reasoning": "",
                    "success": False,
                    "error": str(result),
                })
            else:
                judge_results.append(result)

        # 聚合结果
        aggregated = self._aggregate_scores(judge_results)

        # 理由分析
        reasoning_analysis = {}
        if self.enable_reasoning_analysis:
            reasoning_analysis = self._analyze_reasoning(judge_results)

        return MultiJudgeResult(
            question_index=0,
            judge_results=judge_results,
            aggregated=aggregated,
            reasoning_analysis=reasoning_analysis,
        )

    async def evaluate_batch(
        self,
        dataset: List[Dict[str, Any]],
    ) -> List[MultiJudgeResult]:
        """
        批量多裁判评测

        Args:
            dataset: 评测数据集（支持 V1 和 V2 格式）

        Returns:
            评测结果列表
        """
        results = []

        for idx, item in enumerate(dataset):
            await self._report_progress(idx + 1, len(dataset), f"评测第 {idx + 1}/{len(dataset)} 条")

            # 支持 V1 和 V2 格式
            # V2 格式：数据在 item.data 中
            # V1 格式：数据在 item.question 和 item.ref_answer 中
            if item.get("data") is not None:
                # V2 格式
                data = item.get("data", {})
                question = data.get("question", "") or data.get("user_input", "")
                ref_answer = data.get("ref_answer", "") or data.get("answer", "")
            else:
                # V1 格式
                question = item.get("question", "")
                ref_answer = item.get("ref_answer", "")

            result = await self.evaluate_single(
                question=question,
                ref_answer=ref_answer,
                model_answer=item.get("model_answer", ""),
            )
            result.question_index = item.get("index", idx)
            results.append(result)

        return results
