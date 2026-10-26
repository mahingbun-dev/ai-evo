"""
Judge LangGraph Agent — 节点实现

每个节点是 LangGraph 中的一个处理单元:
- analyze_output: 分析待评测内容
- score_{dim}: 对单个维度独立评分 (工厂函数生成)
- aggregate_scores: 加权聚合 (无 LLM)
- format_output: 格式化输出 (无 LLM)
"""
import json
import re
import logging
from typing import Dict, Any, List
from app.services.judge_pool import judge_pool_slot
from app.services.model_caller import ModelCallerSync
from app.services.prompt_guard import EMPTY_VALUE_RULES, PROMPT_INJECTION_GUARD, wrap_eval_data
from app.services.metric_scoring import (
    format_score_range,
    metric_max_score,
    normalize_dimension_score,
    weight_value,
)
from .judge_graph_state import JudgeAgentState

logger = logging.getLogger(__name__)


def _call_llm(prompt: str, state: JudgeAgentState) -> str:
    """
    调用裁判模型 LLM。

    Args:
        prompt: 发送给 LLM 的完整 prompt (单个 user 消息)
        state: 当前状态 (含 judge_model_config)

    Returns:
        LLM 返回的文本内容
    """
    model_config = state["judge_model_config"]
    if not model_config:
        raise RuntimeError("judge_model_config 为空，无法调用裁判模型")

    messages = [
        # W12-5：system 层声明 <eval_data> 分隔符内是数据非指令（最小 prompt 注入防护）
        {"role": "system", "content": f"你是一个专业的 AI 评测裁判。{PROMPT_INJECTION_GUARD}"},
        {"role": "user", "content": prompt},
    ]

    caller = ModelCallerSync()
    with judge_pool_slot("graph", meta={"call_site": "judge_graph_nodes"}):
        result = caller.call(messages, model_config, {})

    if result.get("success"):
        return result.get("content", "")
    else:
        raise RuntimeError(f"裁判模型调用失败: {result.get('error', '未知错误')}")


def analyze_output_node(state: JudgeAgentState) -> dict:
    """
    分析节点：对待评测内容进行全面分析。

    构建包含场景描述、评测目标和维度的分析 prompt，
    输出分析结果供后续维度评分节点引用。
    """
    scene_desc = state["scene_desc"]
    instruction = state["judge_instruction"]
    targets = state["resolved_targets_content"]
    variables = state["user_variables"]
    metrics = state["evaluation_metrics"]
    max_score = state["max_score"]

    # 构建维度摘要（每个维度标注自己的最高分，低量程维度不再被任务满分误导）
    dim_summaries = []
    for m in metrics:
        name = m.get("name", "")
        desc = m.get("description", "")
        weight = m.get("weight", "中")
        if name:
            dim_max_label = format_score_range(metric_max_score(m, max_score))
            dim_summaries.append(f"- {name} (权重: {weight}, 最高分: {dim_max_label}): {desc}")

    # 构建变量摘要
    var_lines = []
    for v in variables:
        # W12-5：变量值均为外部数据（数据集/模型输出/脚本输出），分隔符包裹
        var_lines.append(f"【{v.get('name', '')}】{wrap_eval_data(v.get('value', ''))}")

    prompt_parts = [
        "你是一个专业的 AI 评测分析助手。你需要全面分析以下待评测内容。",
    ]
    if scene_desc:
        prompt_parts.append(f"\n[评测场景]\n{scene_desc}")
    if instruction:
        prompt_parts.append(f"\n[评测指令]\n{instruction}")
    if targets:
        # W12-5：待评内容用分隔符包裹（数据非指令）
        prompt_parts.append(f"\n[需要评估的内容]\n{wrap_eval_data(targets)}")
    if var_lines:
        prompt_parts.append(f"\n[附加上下文]\n" + "\n\n".join(var_lines))
    if dim_summaries:
        prompt_parts.append(f"\n[评测维度 (总分 0-{max_score} 分，各维度最高分见各项标注)]\n" + "\n".join(dim_summaries))

    # 空值判定规则：约束分析阶段就区分「字段存在但值为空」与「字段缺失」，
    # 避免分析结论带着「字段缺失」的误判污染后续维度评分
    prompt_parts.append(f"\n{EMPTY_VALUE_RULES}")

    prompt_parts.append(
        "\n请分析上述内容的整体质量，并从每个评测维度的角度指出优点和不足。\n"
        "请在 <think> 标签中输出你的分析。"
    )

    prompt = "\n".join(prompt_parts)
    logger.info(f"[analyze_output] prompt 长度: {len(prompt)} 字符")

    analysis = _call_llm(prompt, state)
    logger.info(f"[analyze_output] 分析完成，长度: {len(analysis)} 字符")

    return {"analysis": analysis}


def create_score_dimension_node(metric: dict):
    """
    为单个评测维度创建评分节点的工厂函数。

    Args:
        metric: 维度配置 {name, description, weight, score_criteria}

    Returns:
        评分节点函数 (state -> dict)
    """
    dim_name = metric.get("name", "")
    dim_desc = metric.get("description", "")
    dim_criteria = metric.get("score_criteria", {})

    def score_dimension_node(state: JudgeAgentState) -> dict:
        """
        评分节点：对单个维度进行独立评分。

        使用该维度的评分标准构建 prompt，避免与其他维度交叉干扰。
        如果维度没有配置 score_criteria，回退使用全局 score_details。
        """
        targets = state["resolved_targets_content"]
        analysis = state["analysis"]
        scene_desc = state["scene_desc"]
        max_score = state["max_score"]
        score_details = state["score_details"]
        # 本维度自己的最高分（缺省/非法回退任务满分，与 prompt_builder 口径一致）
        dim_max = metric_max_score(metric, max_score)

        # 构建评分标准段落
        criteria_lines = []
        if dim_criteria:
            # 有精确评分标准
            criteria_lines.append(f"[评分等级]")
            for level, info in dim_criteria.items():
                min_s = info.get("min", 0)
                max_s = info.get("max", dim_max)
                desc = info.get("desc", "")
                criteria_lines.append(f"- {level} ({min_s}-{max_s}分): {desc}")
        elif score_details:
            # 回退全局评分标准
            criteria_lines.append(f"[评分标准]\n{score_details}")
        else:
            criteria_lines.append(f"[评分标准]\n0-{format_score_range(dim_max)}分，越高越好。")

        prompt_parts = [
            f"你是一个 AI 评分裁判。现在需要对**【{dim_name}】**维度进行独立评分。",
        ]
        if scene_desc:
            prompt_parts.append(f"\n[评测场景]\n{scene_desc}")
        if targets:
            # W12-5：待评内容用分隔符包裹（数据非指令）
            prompt_parts.append(f"\n[待评测内容]\n{wrap_eval_data(targets)}")

        # 注入额外变量
        user_vars = state.get("user_variables", [])
        if user_vars:
            var_lines = ["\n[参考数据]"]
            for v in user_vars:
                # W12-5：变量值均为外部数据，分隔符包裹
                var_lines.append(f"【{v.get('name', '')}】{wrap_eval_data(v.get('value', ''))}")
            prompt_parts.append("\n".join(var_lines))

        if analysis:
            prompt_parts.append(f"\n[整体分析参考]\n{analysis}")

        prompt_parts.append(f"\n[当前维度]\n名称: {dim_name}")
        if dim_desc:
            prompt_parts.append(f"定义: {dim_desc}")
        prompt_parts.append("\n".join(criteria_lines))

        # 注入评分步骤上下文：维度顺序即步骤，提示当前维度在整体序列中的位置
        # （契约 v3 §四：不再罗列完整步骤列表，改为「当前是第 i/n 步」上下文提示）
        metrics = state.get("evaluation_metrics", []) or []
        step_total = len(metrics)
        step_index = None
        for idx, m in enumerate(metrics, 1):
            if isinstance(m, dict) and m.get("name") == dim_name:
                step_index = idx
                break
        if step_index is not None and step_total > 0:
            prompt_parts.append(f"\n[评分步骤]\n当前是第 {step_index}/{step_total} 步：{dim_name}")

        # 注入维度依赖规则
        all_deps = state.get("dimension_dependencies", {})
        current_deps = all_deps.get(dim_name, [])
        if current_deps:
            dep_lines = ["\n[维度依赖规则]"]
            for dep in current_deps:
                source = dep.get("source_dimension", "")
                cond = dep.get("condition", "eq")
                val = dep.get("value", 0)
                override = dep.get("override_value", 0)
                cond_text = {"eq": "=", "ne": "!=", "lt": "<", "gt": ">", "lte": "≤", "gte": "≥"}.get(cond, "=")
                dep_lines.append(f"注意：如果「{source}」得分 {cond_text} {val}，本维度得分必须为 {override}。")
            prompt_parts.append("\n".join(dep_lines))

        prompt_parts.append(
            f"\n请严格基于上述标准，对该维度进行评分 (0-{format_score_range(dim_max)}分，"
            f"分数不得超过该维度最高分)。\n"
            f"输出 JSON 格式：\n"
            f'{{"score": <分数>, "reasoning": "<评分理由>"}}\n'
            f"只输出 JSON，不要其他内容。"
        )
        # 空值判定规则：评分阶段是「空值误判为缺失」的高发点，独立提醒一次
        prompt_parts.append(f"\n{EMPTY_VALUE_RULES}")

        prompt = "\n".join(prompt_parts)
        logger.info(f"[score_{dim_name}] prompt 长度: {len(prompt)} 字符")

        raw = _call_llm(prompt, state)

        # 解析 JSON 响应
        try:
            # 提取 JSON 块 (非贪婪正则，避免嵌套代码块干扰)
            json_str = raw.strip()
            code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', json_str)
            if code_block_match:
                json_str = code_block_match.group(1).strip()

            result = json.loads(json_str)
            score = float(result.get("score", 0))
            reasoning = result.get("reasoning", "")
            dim_failed = False
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning(f"[score_{dim_name}] JSON 解析失败，原始响应: {raw[:200]}")
            score = 0.0
            reasoning = "评分解析失败"
            # W2-1 解析失败显式标记：该维度不再以 0 分静默计入加权，
            # 而是携带 judge_failed 标记由 aggregate_scores 冒泡为整裁判失败
            dim_failed = True

        # 确保分数在有效范围（夹到本维度自己的最高分，而非任务满分）
        score = max(0.0, min(score, float(dim_max)))

        dim_result = {
            "score": score,
            "reasoning": reasoning,
        }
        if dim_failed:
            dim_result["judge_failed"] = True
            dim_result["fail_reason"] = "parse_failed"

        return {
            "dimension_results": {dim_name: dim_result},
            "scored_dimensions": [dim_name],
        }

    return score_dimension_node


# 权重文字到数值的映射：统一收敛到 metric_scoring.WEIGHT_MAP /
# weight_value（W4：高/中/低 与 high/medium/low 双向支持，本文件原
# 含首字母大写变体的本地副本已删除，lower 归一后行为覆盖不变）


def _compute_weighted_scores(metrics, dim_results, max_score):
    """计算各维度加权平均分数

    维度分先按各自最高分归一化到任务满分标度再加权（最高分 5 的维度打 5
    与最高分 10 的维度打 10 等价），维度未配置最高分时回退任务满分=恒等，
    存量任务口径不变。反馈文案仍展示原始分。

    Args:
        metrics: 评测维度配置列表
        dim_results: 各维度评分结果
        max_score: 任务满分（归一化目标标度）

    Returns:
        (total_weight, weighted_sum, feedback_parts) 元组
    """
    total_weight = 0.0
    weighted_sum = 0.0
    feedback_parts = []

    for metric in metrics:
        name = metric.get("name", "")

        # 文字权重（高/中/低 与 high/medium/low）统一走
        # metric_scoring.weight_value（W4 收敛点，未知取值兜底 1）
        weight = weight_value(metric.get("weight", 1))

        dim_data = dim_results.get(name, {})
        score = dim_data.get("score", 0)
        reasoning = dim_data.get("reasoning", "")

        normalized = normalize_dimension_score(score, metric, max_score)
        total_weight += weight
        weighted_sum += normalized * weight
        feedback_parts.append(f"【{name}】{score}分 - {reasoning}")

    return total_weight, weighted_sum, feedback_parts


def _evaluate_dependency_condition(condition: str, actual_val: float, threshold_val) -> bool:
    """评估依赖条件是否满足

    Args:
        condition: 条件类型 (eq/ne/lt/gt/lte/gte)
        actual_val: 实际分数
        threshold_val: 条件阈值

    Returns:
        条件是否满足
    """
    if condition == "eq":
        return actual_val == threshold_val
    elif condition == "ne":
        return actual_val != threshold_val
    elif condition == "lt":
        return actual_val < threshold_val
    elif condition == "gt":
        return actual_val > threshold_val
    elif condition == "lte":
        return actual_val <= threshold_val
    elif condition == "gte":
        return actual_val >= threshold_val
    return False


def aggregate_scores_node(state: JudgeAgentState) -> dict:
    """
    聚合节点：按用户配置的权重对各维度评分进行加权平均。

    分三步执行：
    1. 初始加权平均（使用原始分数）
    2. 维度依赖覆盖：如果依赖条件满足，覆盖目标维度分数
    3. 重新计算加权平均（使用覆盖后的分数）
    """
    metrics = state["evaluation_metrics"]
    dim_results = state["dimension_results"]
    max_score = state["max_score"]

    if not metrics or not dim_results:
        return {
            "final_score": 0.0,
            "final_feedback": "没有有效的评分结果",
            "final_dimension_scores": {},
            "final_dimension_feedbacks": {},
        }

    # ── 第一步：初始加权平均（使用原始分数） ──
    total_weight, weighted_sum, feedback_parts = _compute_weighted_scores(metrics, dim_results, max_score)

    if total_weight == 0:
        return {
            "final_score": 0.0,
            "final_feedback": "权重之和为 0",
            "final_dimension_scores": {},
            "final_dimension_feedbacks": {},
        }

    # ── 第二步：维度依赖覆盖逻辑 ──
    dependencies = state.get("dimension_dependencies", {})
    override_applied = False
    for dim_name, deps in dependencies.items():
        dim_data = dim_results.get(dim_name, {})
        if not deps:
            continue
        for dep in deps:
            source_dim = dep.get("source_dimension", "")
            source_score = dim_results.get(source_dim, {}).get("score", None)
            if source_score is None:
                continue
            cond = dep.get("condition", "eq")
            dep_val = dep.get("value", 0)
            override_val = float(dep.get("override_value", 0))

            if _evaluate_dependency_condition(cond, source_score, dep_val):
                current_score = dim_data.get("score", 0)
                if current_score != override_val:
                    dim_data["score"] = override_val
                    dim_data["reasoning"] = dim_data.get("reasoning", "") + (
                        f"（因「{source_dim}」得分满足依赖条件，按规则覆盖为{override_val}分）"
                    )
                    override_applied = True

    # ── 第三步：如果触发了覆盖，重新计算加权平均 ──
    if override_applied:
        total_weight, weighted_sum, feedback_parts = _compute_weighted_scores(metrics, dim_results, max_score)

    final_score = weighted_sum / total_weight
    final_score = max(0.0, min(round(final_score, 2), float(max_score)))

    # ── W2-1: 任一维度解析失败（judge_failed 标记）→ 整个裁判结果标记失败，
    # 行级聚合（_aggregate_judge_scores）据此将该裁判剔除出均分 ──
    final_judge_failed = any(
        dim_data.get("judge_failed")
        for dim_data in dim_results.values()
        if isinstance(dim_data, dict)
    )

    result = {
        "final_score": final_score,
        "final_feedback": "\n\n".join(feedback_parts),
        "final_dimension_scores": {
            name: dim_results.get(name, {}).get("score", 0)
            for name in (m["name"] for m in metrics)
            if name in dim_results
        },
        # 分维度评分理由：仅收录成功评分的维度（judge_failed 维度不进理由表），
        # reasoning 缺失时置空字符串
        "final_dimension_feedbacks": {
            name: dim_results.get(name, {}).get("reasoning") or ""
            for name in (m["name"] for m in metrics)
            if name in dim_results and not dim_results[name].get("judge_failed")
        },
        "final_judge_failed": final_judge_failed,
        "final_fail_reason": "parse_failed" if final_judge_failed else "",
    }
    return result


def format_output_node(state: JudgeAgentState) -> dict:
    """
    格式化节点：将最终结果转换为与 JudgeClient.judge() 兼容的格式。

    无 LLM 调用，纯数据转换。
    """
    output = {
        "score": state["final_score"],
        "feedback": state["final_feedback"],
        "dimension_scores": state["final_dimension_scores"],
        # 分维度评分理由（新增字段；state 缺该键时兜底空 dict，旧消费方无感知）
        "dimension_feedbacks": state.get("final_dimension_feedbacks") or {},
        "judge_details": {
            "analysis": state["analysis"],
            "dimension_details": state["dimension_results"],
        },
    }
    # W2-1: 解析失败裁判的显式失败标记（正常裁判不携带该字段，JSON 结构保持旧形态）
    if state.get("final_judge_failed"):
        output["judge_failed"] = True
        output["fail_reason"] = state.get("final_fail_reason") or "parse_failed"
    return {
        "raw_output": json.dumps(output, ensure_ascii=False),
    }
