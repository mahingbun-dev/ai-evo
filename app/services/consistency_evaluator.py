"""
一致性评测服务
"""
import asyncio
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field

from app.services.judge_evaluation_engine import JudgeEvaluationEngine, WEIGHT_MAP
from app.services.judge_pool import judge_pool_slot
from app.services.prompt_builder import PromptBuilder
from app.services.rule_metrics_calculator import RuleMetricsCalculator
from app.services.response_parser import extract_json_from_response


@dataclass
class ConsistencyResult:
    """一致性评测结果"""
    question_index: int
    repeat_results: List[Dict[str, Any]] = field(default_factory=list)
    judge_assessment: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    evaluation_content: str = ""


class ConsistencyEvaluator:
    """
    一致性评测服务

    对同一评测数据进行多次重复调用，然后评估结果的一致性
    支持与 AI 裁判员一致的裁判配置
    """

    def __init__(
        self,
        model_config: Dict[str, Any],
        judge_config: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
        evaluation_metrics: Optional[List[Dict[str, Any]]] = None,
        judge_variable_config: Optional[Dict[str, Any]] = None,
        dataset_item: Optional[Dict[str, Any]] = None,
        chain_results: Optional[List[Dict[str, Any]]] = None,
        evaluation_targets: Optional[List[Dict[str, Any]]] = None,
        max_score: int = 10,
    ):
        self.model_config = model_config
        self.judge_config = judge_config or model_config
        self.progress_callback = progress_callback
        self.evaluation_metrics = evaluation_metrics or []
        self.max_score = max_score
        self.judge_variable_config = judge_variable_config
        self.dataset_item = dataset_item or {}
        self.chain_results = chain_results or []
        self.evaluation_targets = evaluation_targets
        self.engine = JudgeEvaluationEngine(
            model_config=model_config,
            judge_config=self.judge_config,
            progress_callback=progress_callback,
            judge_variable_config=judge_variable_config,
            dataset_item=dataset_item,
            chain_results=chain_results,
            evaluation_targets=evaluation_targets,
            evaluation_metrics=evaluation_metrics,
        )

    async def _report_progress(self, current: int, total: int, message: str):
        """报告进度"""
        if self.progress_callback:
            try:
                await self.progress_callback(current, total, message)
            except Exception:
                pass

    def _build_legacy_judge_prompt(self, results: List[str]) -> str:
        """构建向后兼容的裁判 prompt（无自定义维度时使用）"""
        formatted_results = self._format_combined_output(results)
        return (
            f"请对以下多次回复进行一致性评测：\n\n{formatted_results}\n\n"
            f"请从以下维度评估：\n"
            f"- 内容一致性\n- 格式一致性\n- 结构一致性"
        )

    def _build_judge_prompt(self, results: List[str]) -> str:
        """构建裁判 prompt（支持自定义维度）"""
        formatted_results = self._format_combined_output(results)
        if self.evaluation_metrics:
            metrics_desc = "\n".join(
                f"- {m['name']}（权重：{m.get('weight', '中')}）"
                for m in self.evaluation_metrics
            )
            return (
                f"请对以下多次回复进行一致性评测：\n\n{formatted_results}\n\n"
                f"评分标准如下：\n{metrics_desc}"
            )
        return self._build_legacy_judge_prompt(results)

    def _extract_runtime_variables(self) -> Dict[str, Any]:
        """从 dataset_item 中提取 runtime_variables"""
        runtime_variables = {}
        if self.dataset_item:
            excluded_keys = {"user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx"}
            for key, value in self.dataset_item.items():
                if key not in excluded_keys:
                    runtime_variables[key] = value

        # 处理 runtime_variables 中的 { value, type } 格式
        for key, val in list(runtime_variables.items()):
            if isinstance(val, dict) and 'value' in val and 'type' in val:
                var_type = val.get('type', 'string')
                var_value = val.get('value', '')
                if var_type == 'number':
                    try:
                        runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                    except (ValueError, TypeError):
                        runtime_variables[key] = 0
                elif var_type == 'boolean':
                    runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                else:
                    runtime_variables[key] = str(var_value)

        return runtime_variables

    def _format_combined_output(self, successful_results: List[str]) -> str:
        """将多次模型回复格式化为 '回复1: ...\n\n回复2: ...' 格式"""
        formatted = []
        for i, r in enumerate(successful_results):
            try:
                parsed = json.loads(r)
                formatted_r = json.dumps(parsed, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, TypeError):
                formatted_r = r
            formatted.append(f"回复{i+1}:\n{formatted_r}")
        return "\n\n".join(formatted)

    def _aggregate_chain_results(self, all_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """聚合多次调用的链结果

        按 index 位置对齐，合并 content 和 variables。
        """
        all_chains = []
        for r in all_results:
            if r.get("success") and r.get("chain_results"):
                all_chains.append(r["chain_results"])
        if not all_chains:
            return []
        max_len = max(len(c) for c in all_chains)
        aggregated = []
        for idx in range(max_len):
            nodes_at_idx = [chain[idx] for chain in all_chains if idx < len(chain)]
            if not nodes_at_idx:
                continue
            base_node = dict(nodes_at_idx[0])
            contents = [n.get("content", "") for n in nodes_at_idx if n.get("content")]
            if len(contents) > 1:
                base_node["content"] = "\n---\n".join(f"第{i+1}次: {c}" for i, c in enumerate(contents))
            elif contents:
                base_node["content"] = contents[0]
            merged_vars = {}
            for node in nodes_at_idx:
                for var_key, var_val in node.get("variables", {}).items():
                    merged_vars.setdefault(var_key, []).append(var_val)
            final_vars = {}
            for var_key, values in merged_vars.items():
                if len(values) == 1:
                    final_vars[var_key] = values[0]
                elif all(isinstance(v, str) for v in values):
                    final_vars[var_key] = "\n---\n".join(f"第{i+1}次: {v}" for i, v in enumerate(values) if v)
                elif all(isinstance(v, list) for v in values):
                    merged = []
                    for v in values:
                        merged.extend(v)
                    final_vars[var_key] = merged
                else:
                    final_vars[var_key] = values
            base_node["variables"] = final_vars
            base_node["_consistency_aggregated"] = True
            base_node["_repeat_count"] = len(nodes_at_idx)
            aggregated.append(base_node)
        return aggregated

    def _get_prompt_builder_config(self) -> Dict[str, Any]:
        """构建传入 PromptBuilder.from_config() 的配置字典"""
        return {
            "judge_variable_config": self.judge_variable_config,
            "evaluation_metrics": self.evaluation_metrics,
            "scene_desc": "评估同一问题多次回复的一致性质量",
            "score_details": "",
            "steps": [
                "1. 理解用户问题和多次回复内容",
                "2. 按照各维度表现进行维度评分",
                "3. 综合评分并说明理由"
            ],
            "max_score": self.max_score,
            "evaluation_targets": self.evaluation_targets,
            "runtime_variables": self._extract_runtime_variables(),
        }

    async def _judge_evaluate(
        self,
        successful_results: List[str],
        all_results: List[Dict[str, Any]],
    ) -> tuple:
        """裁判评估

        Returns:
            (judge_assessment: Dict, evaluation_content: str)
        """
        import logging
        logger = logging.getLogger(__name__)

        combined_output = self._format_combined_output(successful_results)
        aggregated_chain = self._aggregate_chain_results(all_results)

        builder = PromptBuilder.from_config(self._get_prompt_builder_config())

        # 根据 judge_variable_config 解析变量
        if self.judge_variable_config:
            from app.services.judge_variable_resolver import JudgeVariableResolver
            resolver = JudgeVariableResolver(
                task_config={"judge_variable_config": self.judge_variable_config},
                dataset_item=self.dataset_item,
                model_result={"content": combined_output},
                chain_results=aggregated_chain
            )
            resolved_vars = resolver.resolve_all()
            payload = {
                "user_input": resolved_vars.get("user_input", ""),
                "ref_answer": resolved_vars.get("ref_answer", ""),
                "model_output": combined_output,
                # 不再传递 _dataset_item，避免后续 fallback 到数据集字段
            }
        else:
            payload = {
                "user_input": "",
                "ref_answer": "",
                "model_output": combined_output,
                "_dataset_item": self.dataset_item,
            }

        user_prompt = builder.build_user_prompt_from_judge_vars(
            payload=payload,
            model_output=combined_output,
            chain_results=aggregated_chain,
        )

        if not user_prompt:
            user_prompt = f"【模型输出】\n{combined_output}"

        system_prompt = builder._build_system_prompt(
            user_input=payload.get("user_input", ""),
            ref_answer=payload.get("ref_answer", ""),
            model_output=combined_output,
            dataset_item=self.dataset_item if not self.judge_variable_config else {},
            payload=payload,
        )

        # 如果有 evaluation_targets，解析并追加到 system_prompt
        if self.evaluation_targets:
            try:
                from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget
                resolver = JudgeVariableResolver(
                    task_config={},
                    dataset_item=self.dataset_item,
                    model_result={"content": combined_output},
                    chain_results=aggregated_chain,
                )
                targets = [EvaluationTarget(**t) for t in self.evaluation_targets]
                resolved_targets = resolver.resolve_targets(targets)
                targets_content = []
                for target_id, target_info in resolved_targets.items():
                    name = target_info.get("name", "")
                    content = target_info.get("content", "")
                    is_empty = target_info.get("is_empty", True)
                    if content and not is_empty:
                        targets_content.append(f"### {name}\n{content}")
                if targets_content:
                    system_prompt += "\n\n## 评测目标内容\n" + "\n\n".join(targets_content)
            except Exception as e:
                logger.warning(f"解析 evaluation_targets 失败: {e}")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 调试日志：打印发送给裁判模型的关键信息
        logger.info(f"[_judge_evaluate] evaluation_metrics={self.evaluation_metrics}")
        logger.info(f"[_judge_evaluate] system_prompt前500字符={system_prompt[:500]}")

        with judge_pool_slot("graph", meta={"call_site": "consistency_evaluator"}):
            judge_result = await self.engine._call_model(messages, self.judge_config)

        judge_assessment = {}

        if judge_result.get("success"):
            content = judge_result.get("content", "")
            logger.info(f"[_judge_evaluate] 裁判模型原始返回前500字符={content[:500]}")
            scores = extract_json_from_response(content)

            if scores is not None:
                overall_score = scores.get(
                    "综合评分",
                    scores.get("overall_score", scores.get("score", 0))
                )
                reasoning = scores.get(
                    "综合评分原因",
                    scores.get("reasoning", scores.get("feedback", ""))
                )
                dimension_scores = scores.get(
                    "各维度评分",
                    scores.get("dimension_scores", {})
                )
                # 兜底：如果 dimension_scores 为空，尝试从顶层 key 中收集维度评分
                if not dimension_scores and isinstance(scores, dict):
                    for k, v in scores.items():
                        if isinstance(v, dict) and "score" in v:
                            dimension_scores[k] = v

                if isinstance(overall_score, str):
                    try:
                        overall_score = float(overall_score)
                    except (ValueError, TypeError):
                        overall_score = 0.0
                elif not isinstance(overall_score, (int, float)):
                    overall_score = 0.0

                # 按权重强制计算综合评分（复用 JudgeEvaluationEngine 的加权逻辑）
                if dimension_scores and self.evaluation_metrics:
                    calculated_score = JudgeEvaluationEngine.calculate_weighted_score(
                        self.evaluation_metrics, dimension_scores, self.max_score
                    )
                    overall_score = calculated_score

                judge_assessment = {
                    "综合评分": overall_score,
                    "综合评分原因": reasoning,
                    "各维度评分": dimension_scores if isinstance(dimension_scores, dict) else {},
                }
                logger.info(f"裁判评估成功: overall_score={overall_score}")
            else:
                logger.warning(f"裁判评估JSON解析失败: content前200字符={content[:200]}")
                judge_assessment = {
                    "综合评分": 0.0,
                    "综合评分原因": "无法解析评分结果",
                    "各维度评分": {"raw": content[:500]},
                }
        else:
            logger.warning(f"裁判模型调用失败: {judge_result.get('error', 'unknown')}")

        return judge_assessment, combined_output

    async def evaluate_single(
        self,
        question: str,
        repeat_count: int = 5,
        use_judge: bool = True,
    ) -> ConsistencyResult:
        """
        评测单条数据的一致性

        Args:
            question: 用户问题
            repeat_count: 重复次数
            use_judge: 是否使用裁判模型评估

        Returns:
            一致性评测结果
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"evaluate_single 开始: use_judge={use_judge}, evaluation_metrics={self.evaluation_metrics}")

        await self._report_progress(0, repeat_count, f"开始一致性评测，重复{repeat_count}次")

        runtime_variables = self._extract_runtime_variables()

        repeat_results = []

        for i in range(repeat_count):
            await self._report_progress(i + 1, repeat_count, f"第 {i + 1}/{repeat_count} 次调用")

            messages = [{"role": "user", "content": question}]
            result = await self.engine._call_model(messages, self.model_config, runtime_variables=runtime_variables, debug=True)

            repeat_results.append({
                "repeat_id": i + 1,
                "result": result.get("content", "") if result.get("success") else "",
                "success": result.get("success", False),
                "timestamp": "",
                "chain_results": result.get("chain_results", []),
            })

            await asyncio.sleep(0.5)

        successful_results = [r["result"] for r in repeat_results if r["success"]]

        # 规则指标
        metrics = {}
        if successful_results:
            metrics = RuleMetricsCalculator.calculate_all(successful_results)
            metrics["unique_result_count"] = len(set(successful_results))
            metrics["total_calls"] = repeat_count
            metrics["successful_calls"] = len(successful_results)

        # pass^k 指标（M2 QE2-3，语义对齐 τ-bench）：repeat_count 次 trial
        # 的通过序列 → pass@1（单次成功率）/ pass^k（k 次全过比例）与
        # 分数标准差/标准误（trial 分数为 0/1 二值）。全失败行同样产出
        # （pass_at_1=0），裁判不可用不影响确定性统计。
        from app.services.pass_k import score_stats, trial_pass_stats

        passes = [bool(r.get("success")) for r in repeat_results]
        metrics.update(trial_pass_stats(passes))
        metrics.update(score_stats([1.0 if p else 0.0 for p in passes]))

        # 裁判评估
        judge_assessment = {}
        evaluation_content = ""

        if use_judge and self.judge_variable_config and len(successful_results) >= 1:
            judge_assessment, evaluation_content = await self._judge_evaluate(successful_results, repeat_results)

        return ConsistencyResult(
            question_index=0,
            repeat_results=repeat_results,
            judge_assessment=judge_assessment,
            metrics=metrics,
            evaluation_content=evaluation_content,
        )

    async def evaluate_batch(
        self,
        questions: List[str],
        repeat_count: int = 5,
        use_judge: bool = True,
    ) -> List[ConsistencyResult]:
        """
        批量一致性评测

        Args:
            questions: 问题列表
            repeat_count: 重复次数
            use_judge: 是否使用裁判模型

        Returns:
            一致性评测结果列表
        """
        results = []

        for idx, question in enumerate(questions):
            await self._report_progress(idx + 1, len(questions), f"评测第 {idx + 1}/{len(questions)} 条")

            result = await self.evaluate_single(question, repeat_count, use_judge)
            result.question_index = idx + 1
            results.append(result)

        return results
