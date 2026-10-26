"""
AI裁判员评测引擎
用于对通用AI模型进行评测
"""
import asyncio
import json
import uuid
import time
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from jinja2.sandbox import SandboxedEnvironment

from app.services.model_caller import ModelCaller
from app.services.instruction_builder import InstructionBuilder
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget
from app.services.judge_validation_service import JudgeValidationService
from app.services.metric_scoring import WEIGHT_MAP, normalize_dimension_score, weight_value

# WEIGHT_MAP 自 metric_scoring 统一（W4：中/英文双向支持）；
# 此处 re-export 保持 consistency_evaluator 等存量 import 路径不变。


@dataclass
class JudgeEvaluationResult:
    """裁判评测结果"""
    question_index: int
    question: str
    ref_answer: str
    model_answer: str
    scores: Dict[str, Any] = field(default_factory=dict)
    overall_score: float = 0.0
    reasoning: str = ""
    judge_model: str = ""
    success: bool = True
    error: str = ""
    user_input: str = ""  # 用户原始输入
    duration: float = 0.0  # 评测耗时（秒）
    # 多裁判独立记录字段
    judge_index: int = 0
    evaluation_target_id: str = ""
    evaluation_target_name: str = ""
    validation_message: str = ""


@dataclass
class EvaluationDatasetItem:
    """评测数据项"""
    index: int
    question: str
    ref_answer: str
    attachments: Optional[List[Dict]] = None


class JudgeEvaluationEngine:
    """
    AI裁判员评测引擎

    支持：
    - 多种模型接入（OpenAI、Anthropic、自定义HTTP）
    - 多模态评测
    - 自定义评测维度
    """

    def __init__(
        self,
        model_config: Dict[str, Any],
        judge_config: Dict[str, Any],
        instruction_template: str = "",
        progress_callback: Optional[Callable] = None,
        judge_variable_config: Optional[Dict[str, Any]] = None,
        dataset_item: Optional[Dict[str, Any]] = None,
        chain_results: Optional[List[Dict[str, Any]]] = None,
        evaluation_targets: Optional[List[Dict[str, Any]]] = None,
        validation_enabled: bool = True,
        evaluation_metrics: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        初始化评测引擎

        Args:
            model_config: 被评测模型配置
            judge_config: 裁判模型配置
            instruction_template: 评测指令模板
            progress_callback: 进度回调函数
            judge_variable_config: 裁判变量配置（数据来源和prompt类型）
            dataset_item: 数据集项（用于变量解析）
            chain_results: 请求链结果列表
            evaluation_targets: 评测目标配置列表
            validation_enabled: 是否启用空值校验
            evaluation_metrics: 自定义评测维度配置
        """
        self.model_config = model_config
        self.judge_config = judge_config
        self.instruction_template = instruction_template
        self.progress_callback = progress_callback
        self.judge_variable_config = judge_variable_config
        self.dataset_item = dataset_item or {}
        self.chain_results = chain_results or []
        self.evaluation_targets = evaluation_targets
        self.validation_enabled = validation_enabled
        self.evaluation_metrics = evaluation_metrics or []
        self._max_score = 10  # 默认最大分值，可被外部覆盖

        # 默认评测模板
        self.default_template = instruction_template or InstructionBuilder().build({
            "scene": "通用",
            "scene_description": "评测AI回复质量",
            "evaluation_metrics": [],
            "max_score": 10,
            "steps": [],
        })

    async def _call_model(
        self,
        messages: List[Dict],
        config: Dict[str, Any],
        is_multimodal: bool = False,
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """
        调用模型

        Args:
            messages: 消息列表
            config: 模型配置
            is_multimodal: 是否多模态
            runtime_variables: 运行时变量（用于请求链变量替换）
            debug: 是否返回详细的请求链执行信息

        Returns:
            模型响应
        """
        # 使用通用模型调用器
        result = await ModelCaller.call(messages, config, runtime_variables=runtime_variables, debug=debug)
        return result

    def _build_judge_prompt(
        self,
        question: str,
        ref_answer: str,
        model_answer: str,
    ) -> List[Dict]:
        """构建裁判prompt

        Args:
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            消息列表
        """
        # 如果有变量配置，使用新的解析方式
        if self.judge_variable_config:
            return self._build_judge_prompt_with_config(question, ref_answer, model_answer)

        # 兼容旧版本：使用默认模板替换变量
        prompt = self.default_template
        prompt = prompt.replace("{{question}}", question)
        prompt = prompt.replace("{{ref_answer}}", ref_answer)
        prompt = prompt.replace("{{answer}}", model_answer)

        return [
            {"role": "system", "content": "你是一名公正的评分裁判。"},
            {"role": "user", "content": prompt},
        ]

    def _build_judge_prompt_with_config(
        self,
        question: str,
        ref_answer: str,
        model_answer: str,
    ) -> List[Dict]:
        """使用变量配置构建裁判prompt

        根据 judge_variable_config 配置决定：
        1. 变量值从哪个数据来源获取（已在外部解析后传入）
        2. 变量放在 system 还是 user 消息中

        Args:
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            消息列表
        """
        # 构建变量解析器
        task_config = {
            "judge_variable_config": self.judge_variable_config,
            "runtime_variables": {}  # 如果需要手动输入变量可以传入
        }

        # 注意：不再强制从 dataset_item 覆盖 question 和 ref_answer
        # JudgeVariableResolver 会根据 judge_variable_config 正确解析变量
        # 如果 judge_variable_config 未配置，resolved 值将为空字符串

        resolver = JudgeVariableResolver(
            task_config=task_config,
            dataset_item=self.dataset_item,
            model_result={"content": model_answer},
            chain_results=self.chain_results
        )

        # 使用resolver构建消息
        messages = resolver.build_messages(system_role="你是一名公正的评分裁判。")

        # 如果有自定义评测维度，将维度信息添加到 system prompt 中（与 AI 裁判员一致）
        if self.evaluation_metrics:
            from app.services.instruction_builder import InstructionBuilder
            builder = InstructionBuilder()
            metrics_text = builder._build_metrics(self.evaluation_metrics)
            score_details = builder._build_score_details(self.evaluation_metrics)

            dimension_system_prompt = f"""

## 评测维度
{metrics_text}

## 评分标准
{score_details}

## 输出格式要求
请以JSON格式返回：
{{
    "综合评分": "加权平均得分",
    "综合评分原因": "总结评价",
    "各维度评分": {{
        "维度1名称": {{"score": 分数, "analysis": "分析"}},
        "维度2名称": {{"score": 分数, "analysis": "分析"}}
    }}
}}
"""
            # 将维度信息追加到 system 消息
            if messages[0]["role"] == "system":
                messages[0]["content"] += dimension_system_prompt

        return messages

    def _build_prompt_with_placeholders(
        self,
        resolved_targets: Dict[str, Dict[str, Any]],
        system_template: str,
        user_template: str
    ) -> List[Dict[str, Any]]:
        """使用Jinja2模板渲染构建提示词

        Args:
            resolved_targets: 解析后的评测目标字典
            system_template: System prompt 模板
            user_template: User prompt 模板

        Returns:
            消息列表
        """
        targets_list = list(resolved_targets.values())
        # 模板来自用户配置（评测提示词模板），必须用沙箱环境渲染，
        # 阻断 {{ ''.__class__ }} 之类逃逸到 Python 对象模型的写法
        env = SandboxedEnvironment(autoescape=False)
        system_content = env.from_string(system_template).render(targets=targets_list)
        user_content = env.from_string(user_template).render(targets=targets_list)
        return [
            {"role": "system", "content": system_content.strip()},
            {"role": "user", "content": user_content.strip()}
        ]

    def _has_string_scores(self, dimension_scores: Dict[str, Any]) -> bool:
        """
        检查维度评分中是否有字符串类型的分数

        Args:
            dimension_scores: 各维度评分字典

        Returns:
            True if any score is a string
        """
        for dim_name, dim_data in dimension_scores.items():
            if isinstance(dim_data, dict):
                score_val = dim_data.get("score") or dim_data.get("dimension_score")
                if isinstance(score_val, str):
                    return True
            elif isinstance(dim_data, str):
                return True
        return False

    def _validate_and_clean_dimension_scores(self, scores: Dict[str, Any]) -> Dict[str, Any]:
        """
        验证并清洗各维度评分数据

        当裁判模型返回错误格式的数据时（如在"各维度评分"中放入模型元数据），
        此方法会过滤掉无效数据，确保只保留正确的维度评分。

        Args:
            scores: 原始评分字典

        Returns:
            清洗后的评分字典
        """
        if not isinstance(scores, dict):
            return scores

        # 检查是否有字符串类型的分数需要转换
        dimension_scores = scores.get("各维度评分")
        if not dimension_scores or not isinstance(dimension_scores, dict):
            return scores

        # 检查是否包含模型元数据字段
        model_metadata_fields = {
            "id", "name", "provider", "model_type", "endpoint", "auth_type",
            "api_key", "auth_config", "request_config", "response_parse_config",
            "supports_multimodal", "description", "role_description", "is_active",
            "is_preset", "created_at", "model_id", "model_role"
        }

        dimension_keys = set(dimension_scores.keys())
        metadata_keys = dimension_keys & model_metadata_fields

        # 即使没有模型元数据字段，也要检查并转换字符串类型的分数
        needs_cleaning = metadata_keys or self._has_string_scores(dimension_scores)

        if needs_cleaning:
            import logging
            logger = logging.getLogger(__name__)

            if metadata_keys:
                logger.warning(
                    f"[JudgeEvaluationEngine] 检测到各维度评分包含模型元数据字段: {metadata_keys}，"
                    f"正在清洗数据。原始keys: {dimension_keys}"
                )

            # 过滤掉模型元数据字段，保留可能的有效维度评分
            cleaned_dims = {}
            for key, value in dimension_scores.items():
                if key in model_metadata_fields:
                    continue
                # 验证这个key对应的value是否是有效的维度评分格式
                if isinstance(value, dict):
                    # 有效的维度评分应该是 {"score": number, "analysis": "..."} 格式
                    if "score" in value or "dimension_score" in value:
                        score_val = value.get("score") or value.get("dimension_score")
                        # 如果 score 是字符串，尝试转换为数字
                        if isinstance(score_val, str):
                            try:
                                score_val = float(score_val)
                            except (ValueError, TypeError):
                                pass
                        if isinstance(score_val, (int, float)):
                            cleaned_dims[key] = {
                                "score": score_val,
                                "analysis": value.get("analysis", "")
                            }
                elif isinstance(value, (int, float)):
                    # 直接是数字也是有效的
                    cleaned_dims[key] = {"score": float(value)}
                elif isinstance(value, str):
                    # 如果是字符串，尝试转换为数字
                    try:
                        cleaned_dims[key] = {"score": float(value)}
                    except (ValueError, TypeError):
                        pass

            if cleaned_dims:
                scores["各维度评分"] = cleaned_dims
                logger.info(f"[JudgeEvaluationEngine] 清洗后保留的维度评分: {list(cleaned_dims.keys())}")
            else:
                # 没有有效的维度评分，标记为空字典
                scores["各维度评分"] = {}
                logger.warning(f"[JudgeEvaluationEngine] 各维度评分无有效数据，已清空")

        return scores

    @staticmethod
    def calculate_weighted_score(evaluation_metrics, dimension_scores, max_score):
        """
        按权重加权平均计算综合评分

        维度分先按各自最高分（evaluation_metrics[].max_score）归一化到任务
        满分标度再加权：最高分 5 的维度打 5 与最高分 10 的维度打 10 等价；
        维度未配置最高分时回退任务满分=恒等变换，存量任务口径不变。

        Args:
            evaluation_metrics: 评测维度配置列表，每个元素需含 name 和 weight
            dimension_scores: 各维度评分字典 {"维度名": {"score": 数字}, ...}
            max_score: 最高分上限

        Returns:
            加权平均后的综合评分（round 到 2 位小数）
        """
        if not evaluation_metrics or not dimension_scores:
            return 0.0

        total_weight = 0.0
        weighted_sum = 0.0

        for metric in evaluation_metrics:
            name = metric.get("name", "")

            # 支持文字权重（高/中/低 与 high/medium/low）和数值权重，
            # 统一走 metric_scoring.weight_value（W4 收敛点，未知取值兜底 1）
            weight = weight_value(metric.get("weight", 1))

            # 从 dimension_scores 中获取对应维度的评分
            dim_data = dimension_scores.get(name, {})
            if isinstance(dim_data, dict):
                score = dim_data.get("score", dim_data.get("dimension_score", 0))
            else:
                score = dim_data

            if isinstance(score, str):
                try:
                    score = float(score)
                except (ValueError, TypeError):
                    score = 0

            # 按维度最高分归一化到任务满分标度（缺省回退任务满分=恒等）
            normalized = normalize_dimension_score(score, metric, max_score)
            total_weight += weight
            weighted_sum += normalized * weight

        if total_weight == 0:
            return 0.0

        result = weighted_sum / total_weight
        result = max(0.0, min(result, max_score))
        return round(result, 2)

    def _calculate_weighted_score(self, dimension_scores):
        """
        实例方法，委托到 calculate_weighted_score 静态方法

        Args:
            dimension_scores: 各维度评分字典

        Returns:
            加权平均后的综合评分
        """
        return self.calculate_weighted_score(
            self.evaluation_metrics, dimension_scores, self._max_score
        )

    async def _report_progress(self, current: int, total: int, message: str):
        """报告进度"""
        if self.progress_callback:
            try:
                await self.progress_callback(current, total, message)
            except Exception:
                pass

    async def evaluate_single(
        self,
        question: str,
        ref_answer: str,
        model_answer: str,
    ) -> JudgeEvaluationResult:
        """
        评测单条数据

        Args:
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            评测结果
        """
        # 空值校验：模型回答为空时直接返回零分，避免裁判幻觉
        if not model_answer or not model_answer.strip():
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer or "",
                scores={
                    "综合评分": 0,
                    "校验状态": "failed",
                    "校验失败原因": "模型回答内容为空，无法进行有效评估",
                },
                overall_score=0.0,
                reasoning="模型回答内容为空，跳过裁判评测",
                judge_model=self.judge_config.get("model_type", ""),
                validation_message="模型回答内容为空",
            )

        # 如果配置了 evaluation_targets，使用新的多评测目标逻辑
        if self.evaluation_targets:
            return await self._evaluate_with_targets(question, ref_answer, model_answer)

        # 构建prompt
        messages = self._build_judge_prompt(question, ref_answer, model_answer)

        # 调用裁判模型
        result = await self._call_model(messages, self.judge_config)

        if not result.get("success"):
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                success=False,
                error=result.get("error", "未知错误"),
            )

        # 解析评分结果
        content = result.get("content", "")
        try:
            # 尝试解析JSON
            scores = json.loads(content)
            # 处理LLM返回列表格式的情况
            if isinstance(scores, list):
                if scores and isinstance(scores[0], dict):
                    scores = scores[0]
                else:
                    scores = {}
            overall_score = scores.get("综合评分", scores.get("overall_score", 0))
            reasoning = scores.get("综合评分原因", scores.get("reasoning", ""))

            # 验证和清洗各维度评分
            scores = self._validate_and_clean_dimension_scores(scores)

            # 按权重强制计算综合评分（覆盖 LLM 返回值）
            dimension_scores = scores.get("各维度评分", scores.get("dimension_scores", {}))
            if dimension_scores and self.evaluation_metrics:
                calculated_score = self._calculate_weighted_score(dimension_scores)
                overall_score = calculated_score

            # 确保 overall_score 是有效的数字
            if isinstance(overall_score, str):
                try:
                    overall_score = float(overall_score)
                except (ValueError, TypeError):
                    overall_score = 0.0
            elif not isinstance(overall_score, (int, float)):
                overall_score = 0.0

            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                scores=scores,
                overall_score=float(overall_score) if overall_score else 0.0,
                reasoning=reasoning,
                judge_model=self.judge_config.get("model_type", ""),
            )
        except json.JSONDecodeError:
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                success=True,
                scores={"raw": content},
                overall_score=0.0,
                reasoning="无法解析评分结果",
                judge_model=self.judge_config.get("model_type", ""),
            )

    async def _evaluate_with_targets(
        self,
        question: str,
        ref_answer: str,
        model_answer: str,
    ) -> JudgeEvaluationResult:
        """使用多评测目标进行评测

        Args:
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            评测结果
        """
        # 空值校验：模型回答为空时直接返回零分，避免裁判幻觉
        if not model_answer or not model_answer.strip():
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer or "",
                scores={
                    "综合评分": 0,
                    "校验状态": "failed",
                    "校验失败原因": "模型回答内容为空，无法进行有效评估",
                },
                overall_score=0.0,
                reasoning="模型回答内容为空，跳过裁判评测",
                judge_model=self.judge_config.get("model_type", ""),
                validation_message="模型回答内容为空",
            )

        # 解析评测目标
        targets = [EvaluationTarget(**t) for t in self.evaluation_targets]

        # 构建 task_config，确保包含 judge_variable_config
        task_config = {}
        if self.judge_variable_config:
            task_config["judge_variable_config"] = self.judge_variable_config

        resolver = JudgeVariableResolver(
            task_config=task_config,
            dataset_item=self.dataset_item,
            model_result={"content": model_answer},
            chain_results=self.chain_results
        )
        resolved_targets = resolver.resolve_targets(targets)

        # 空值校验
        validation_service = JudgeValidationService()
        is_valid, validation_results = validation_service.validate_targets(
            resolved_targets, self.validation_enabled
        )

        # 校验失败时返回零分
        if not is_valid:
            zero_result = validation_service.build_zero_score_result(
                judge_model=self.judge_config.get("model_type", ""),
                validation_results=validation_results,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer
            )
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                scores=zero_result.get("scores", {}),
                overall_score=0.0,
                reasoning=zero_result.get("reasoning", ""),
                judge_model=self.judge_config.get("model_type", ""),
                validation_message=zero_result.get("validation_message", ""),
            )

        # 检查是否配置了 Jinja2 模板
        system_template = self.instruction_template.get("system_prompt_template") if isinstance(self.instruction_template, dict) else None
        user_template = self.instruction_template.get("user_prompt_template") if isinstance(self.instruction_template, dict) else None

        if system_template and user_template:
            # 使用 Jinja2 模板渲染
            messages = self._build_prompt_with_placeholders(
                resolved_targets, system_template, user_template
            )
        else:
            # 回退到原有逻辑
            messages = self._build_judge_prompt(question, ref_answer, model_answer)

        # 调用裁判模型
        result = await self._call_model(messages, self.judge_config)

        if not result.get("success"):
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                success=False,
                error=result.get("error", "未知错误"),
            )

        # 解析评分结果
        content = result.get("content", "")
        try:
            scores = json.loads(content)
            overall_score = scores.get("综合评分", scores.get("overall_score", 0))
            reasoning = scores.get("综合评分原因", scores.get("reasoning", ""))

            # 验证和清洗各维度评分
            scores = self._validate_and_clean_dimension_scores(scores)

            # 按权重强制计算综合评分（覆盖 LLM 返回值）
            dimension_scores = scores.get("各维度评分", scores.get("dimension_scores", {}))
            if dimension_scores and self.evaluation_metrics:
                calculated_score = self._calculate_weighted_score(dimension_scores)
                overall_score = calculated_score

            # 确保 overall_score 是有效的数字
            if isinstance(overall_score, str):
                try:
                    overall_score = float(overall_score)
                except (ValueError, TypeError):
                    overall_score = 0.0
            elif not isinstance(overall_score, (int, float)):
                overall_score = 0.0

            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                scores=scores,
                overall_score=float(overall_score) if overall_score else 0.0,
                reasoning=reasoning,
                judge_model=self.judge_config.get("model_type", ""),
            )
        except json.JSONDecodeError:
            return JudgeEvaluationResult(
                question_index=1,
                question=question,
                ref_answer=ref_answer,
                model_answer=model_answer,
                success=True,
                scores={"raw": content},
                overall_score=0.0,
                reasoning="无法解析评分结果",
                judge_model=self.judge_config.get("model_type", ""),
            )

    async def evaluate_batch(
        self,
        dataset: List[EvaluationDatasetItem],
    ) -> List[JudgeEvaluationResult]:
        """
        批量评测

        Args:
            dataset: 评测数据集

        Returns:
            评测结果列表
        """
        results = []
        total = len(dataset)

        for idx, item in enumerate(dataset):
            await self._report_progress(idx + 1, total, f"评测第 {idx + 1}/{total} 条")

            # 调用被评测模型
            model_messages = [{"role": "user", "content": item.question}]
            model_result = await self._call_model(model_messages, self.model_config)

            if not model_result.get("success"):
                results.append(JudgeEvaluationResult(
                    question_index=item.index,
                    question=item.question,
                    ref_answer=item.ref_answer,
                    model_answer="",
                    success=False,
                    error=f"模型调用失败: {model_result.get('error')}",
                ))
                continue

            model_answer = model_result.get("content", "")

            # 裁判评测
            judge_result = await self.evaluate_single(
                question=item.question,
                ref_answer=item.ref_answer,
                model_answer=model_answer,
            )
            judge_result.question_index = item.index + 1

            results.append(judge_result)

        return results

