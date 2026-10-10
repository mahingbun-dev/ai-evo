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
from app.services.model_caller import ModelCallerSync
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
        {"role": "system", "content": "你是一个专业的 AI 评测裁判。"},
        {"role": "user", "content": prompt},
    ]

    caller = ModelCallerSync()
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

    # 构建维度摘要
    dim_summaries = []
    for m in metrics:
        name = m.get("name", "")
        desc = m.get("description", "")
        weight = m.get("weight", "中")
        if name:
            dim_summaries.append(f"- {name} (权重: {weight}): {desc}")

    # 构建变量摘要
    var_lines = []
    for v in variables:
        var_lines.append(f"【{v.get('name', '')}】{v.get('value', '')}")

    prompt_parts = [
        "你是一个专业的 AI 评测分析助手。你需要全面分析以下待评测内容。",
    ]
    if scene_desc:
        prompt_parts.append(f"\n[评测场景]\n{scene_desc}")
    if instruction:
        prompt_parts.append(f"\n[评测指令]\n{instruction}")
    if targets:
        prompt_parts.append(f"\n[需要评估的内容]\n{targets}")
    if var_lines:
        prompt_parts.append(f"\n[附加上下文]\n" + "\n\n".join(var_lines))
    if dim_summaries:
        prompt_parts.append(f"\n[评测维度 (0-{max_score}分)]\n" + "\n".join(dim_summaries))

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

        # 构建评分标准段落
        criteria_lines = []
        if dim_criteria:
            # 有精确评分标准
            criteria_lines.append(f"[评分等级]")
            for level, info in dim_criteria.items():
                min_s = info.get("min", 0)
                max_s = info.get("max", max_score)
                desc = info.get("desc", "")
                criteria_lines.append(f"- {level} ({min_s}-{max_s}分): {desc}")
        elif score_details:
            # 回退全局评分标准
            criteria_lines.append(f"[评分标准]\n{score_details}")
        else:
            criteria_lines.append(f"[评分标准]\n0-{max_score}分，越高越好。")

        prompt_parts = [
            f"你是一个 AI 评分裁判。现在需要对**【{dim_name}】**维度进行独立评分。",
        ]
        if scene_desc:
            prompt_parts.append(f"\n[评测场景]\n{scene_desc}")
        if targets:
            prompt_parts.append(f"\n[待评测内容]\n{targets}")

        # 注入额外变量
        user_vars = state.get("user_variables", [])
        if user_vars:
            var_lines = ["\n[参考数据]"]
            for v in user_vars:
                var_lines.append(f"【{v.get('name', '')}】{v.get('value', '')}")
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
            f"\n请严格基于上述标准，对该维度进行评分 (0-{max_score}分)。\n"
            f"输出 JSON 格式：\n"
            f'{{"score": <分数>, "reasoning": "<评分理由>"}}\n'
            f"只输出 JSON，不要其他内容。"
        )

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
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning(f"[score_{dim_name}] JSON 解析失败，原始响应: {raw[:200]}")
            score = 0.0
            reasoning = "评分解析失败"

        # 确保分数在有效范围
        score = max(0.0, min(score, float(max_score)))

        return {
            "dimension_results": {
                dim_name: {
                    "score": score,
                    "reasoning": reasoning,
                }
            },
            "scored_dimensions": [dim_name],
        }

    return score_dimension_node


# 权重文字到数值的映射
WEIGHT_MAP = {
    "高": 3, "中": 2, "低": 1,
    "high": 3, "medium": 2, "low": 1,
    "High": 3, "Medium": 2, "Low": 1,
}


def _compute_weighted_scores(metrics, dim_results):
    """计算各维度加权平均分数

    Args:
        metrics: 评测维度配置列表
        dim_results: 各维度评分结果

    Returns:
        (total_weight, weighted_sum, feedback_parts) 元组
    """
    total_weight = 0.0
    weighted_sum = 0.0
    feedback_parts = []

    for metric in metrics:
        name = metric.get("name", "")
        weight = metric.get("weight", 1)

        if isinstance(weight, str):
            weight = WEIGHT_MAP.get(weight, 1)

        dim_data = dim_results.get(name, {})
        score = dim_data.get("score", 0)
        reasoning = dim_data.get("reasoning", "")

        total_weight += weight
        weighted_sum += score * weight
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
        }

    # ── 第一步：初始加权平均（使用原始分数） ──
    total_weight, weighted_sum, feedback_parts = _compute_weighted_scores(metrics, dim_results)

    if total_weight == 0:
        return {
            "final_score": 0.0,
            "final_feedback": "权重之和为 0",
            "final_dimension_scores": {},
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
        total_weight, weighted_sum, feedback_parts = _compute_weighted_scores(metrics, dim_results)

    final_score = weighted_sum / total_weight
    final_score = max(0.0, min(round(final_score, 2), float(max_score)))

    return {
        "final_score": final_score,
        "final_feedback": "\n\n".join(feedback_parts),
        "final_dimension_scores": {
            name: dim_results.get(name, {}).get("score", 0)
            for name in (m["name"] for m in metrics)
            if name in dim_results
        },
    }


def format_output_node(state: JudgeAgentState) -> dict:
    """
    格式化节点：将最终结果转换为与 JudgeClient.judge() 兼容的格式。

    无 LLM 调用，纯数据转换。
    """
    return {
        "raw_output": json.dumps({
            "score": state["final_score"],
            "feedback": state["final_feedback"],
            "dimension_scores": state["final_dimension_scores"],
            "judge_details": {
                "analysis": state["analysis"],
                "dimension_details": state["dimension_results"],
            },
        }, ensure_ascii=False),
    }
