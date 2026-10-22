"""复合分引擎：维度权重 × 层混合 × 反模式惩罚 → 0-100 复合分与徽章。

移植自 plugin-eval src/plugin_eval/engine.py 的复合分部分（_build_composite /
_blend_layer_scores / _assemble_composite / Badge / grade），输出纯 dict。
某层缺席（如未配置裁判模型时 judge unmeasured）时权重再归一化，评测仍可用。
"""

from __future__ import annotations

from app.services.plugin_eval.static_layer import anti_pattern_penalty

# 顶层维度权重（和为 1.0）
DIMENSION_WEIGHTS = {
    "triggering_accuracy": 0.25,
    "orchestration_fitness": 0.20,
    "output_quality": 0.15,
    "scope_calibration": 0.12,
    "progressive_disclosure": 0.10,
    "token_efficiency": 0.06,
    "robustness": 0.05,
    "structural_completeness": 0.03,
    "code_template_quality": 0.02,
    "ecosystem_coherence": 0.02,
}

# 各维度跨层（static/judge/monte_carlo）混合权重；
# 列有权重的格子也可能缺数据——混合时只用实际可用的层并再归一化
LAYER_BLENDS = {
    "triggering_accuracy": {"static": 0.15, "judge": 0.25, "monte_carlo": 0.60},
    "orchestration_fitness": {"static": 0.10, "judge": 0.70, "monte_carlo": 0.20},
    "output_quality": {"static": 0.00, "judge": 0.40, "monte_carlo": 0.60},
    "scope_calibration": {"static": 0.30, "judge": 0.55, "monte_carlo": 0.15},
    "progressive_disclosure": {"static": 0.80, "judge": 0.20, "monte_carlo": 0.00},
    "token_efficiency": {"static": 0.40, "judge": 0.10, "monte_carlo": 0.50},
    "robustness": {"static": 0.00, "judge": 0.20, "monte_carlo": 0.80},
    "structural_completeness": {"static": 0.90, "judge": 0.10, "monte_carlo": 0.00},
    "code_template_quality": {"static": 0.30, "judge": 0.70, "monte_carlo": 0.00},
    "ecosystem_coherence": {"static": 0.85, "judge": 0.15, "monte_carlo": 0.00},
}

# 静态子分名 → 维度名映射
STATIC_TO_DIMENSION = {
    "frontmatter_quality": "triggering_accuracy",
    "orchestration_wiring": "orchestration_fitness",
    "structural_completeness": "structural_completeness",
    "progressive_disclosure": "progressive_disclosure",
    "token_efficiency": "token_efficiency",
    "ecosystem_coherence": "ecosystem_coherence",
}

# 置信标签随评测深度变化（诚实标注评测可信度）
DEPTH_CONFIDENCE_LABELS = {
    "quick": "Estimated",
    "standard": "Assessed",
    "deep": "Certified",
    "thorough": "Certified+",
}

# 维度中文名（前端展示用）
DIMENSION_LABELS_ZH = {
    "triggering_accuracy": "触发准确性",
    "orchestration_fitness": "编排适配度",
    "output_quality": "输出质量",
    "scope_calibration": "范围校准",
    "progressive_disclosure": "渐进式披露",
    "token_efficiency": "Token 效率",
    "robustness": "健壮性",
    "structural_completeness": "结构完整性",
    "code_template_quality": "代码模板质量",
    "ecosystem_coherence": "生态一致性",
}


def score_to_grade(score: float) -> str:
    """0-100 分转字母等级。"""
    if score >= 97:
        return "A+"
    elif score >= 93:
        return "A"
    elif score >= 90:
        return "A-"
    elif score >= 87:
        return "B+"
    elif score >= 83:
        return "B"
    elif score >= 80:
        return "B-"
    elif score >= 77:
        return "C+"
    elif score >= 73:
        return "C"
    elif score >= 70:
        return "C-"
    elif score >= 67:
        return "D+"
    elif score >= 63:
        return "D"
    elif score >= 60:
        return "D-"
    else:
        return "F"


def badge_from_score(composite: float) -> str:
    """0-100 复合分转徽章：Platinum≥90 / Gold≥80 / Silver≥70 / Bronze≥60。"""
    if composite >= 90:
        return "platinum"
    elif composite >= 80:
        return "gold"
    elif composite >= 70:
        return "silver"
    elif composite >= 60:
        return "bronze"
    return "no_badge"


def build_composite(layers: list, depth: str = "quick") -> dict:
    """从可用的层结果构建复合分 dict（CompositeResult 同构）。"""
    static_result = next((lr for lr in layers if lr.get("layer") == "static"), None)
    judge_result = next((lr for lr in layers if lr.get("layer") == "judge"), None)

    static_scores = _map_static_to_dimensions(static_result) if static_result else None
    judge_scores = judge_result.get("sub_scores") if judge_result else None

    dimension_scores = _blend_layer_scores(
        static_scores=static_scores,
        judge_scores=judge_scores,
    )

    anti_pattern_count = len(static_result.get("anti_patterns", [])) if static_result else 0
    return _assemble_composite(dimension_scores, anti_pattern_count, depth)


def build_composite_from_static_overall(static_score: float, anti_pattern_count: int, depth: str = "quick") -> dict:
    """插件级整体评测的复合分：静态总分映射到全部静态可测维度。

    与 plugin-eval evaluate_plugin 同口径——插件级结果没有维度子分明细，
    用整体静态分填充 STATIC_TO_DIMENSION 覆盖的维度，其余维度视为未测。
    """
    dimension_scores = {dim: static_score for dim in STATIC_TO_DIMENSION.values()}
    return _assemble_composite(dimension_scores, anti_pattern_count, depth)


def _assemble_composite(dimension_scores: dict, anti_pattern_count: int, depth: str) -> dict:
    """按混合后的维度分与反模式数组装复合分。"""
    penalty = anti_pattern_penalty(anti_pattern_count)

    # 已测维度（>=0）与未测维度（-1.0 哨兵 / 完全缺席）
    measured = {d: s for d, s in dimension_scores.items() if s >= 0.0}
    unmeasured = {d for d, s in dimension_scores.items() if s < 0.0} | (
        set(DIMENSION_WEIGHTS) - set(dimension_scores)
    )

    # 只对已测维度做权重再归一化
    measured_weight_sum = sum(DIMENSION_WEIGHTS.get(d, 0.0) for d in measured)
    if measured_weight_sum > 0:
        raw = sum(
            (DIMENSION_WEIGHTS.get(dim, 0.0) / measured_weight_sum) * score
            for dim, score in measured.items()
        )
    else:
        raw = 0.0
    composite_score = min(100.0, max(0.0, raw * 100.0 * penalty))

    dim_objects = []
    for dim in DIMENSION_WEIGHTS:
        weight = DIMENSION_WEIGHTS[dim]
        if dim in unmeasured:
            dim_objects.append(
                {
                    "name": dim,
                    "label": DIMENSION_LABELS_ZH.get(dim, dim),
                    "weight": weight,
                    "score": None,
                    "grade": "—",
                }
            )
        else:
            score = measured.get(dim, 0.0)
            dim_objects.append(
                {
                    "name": dim,
                    "label": DIMENSION_LABELS_ZH.get(dim, dim),
                    "weight": weight,
                    "score": score,
                    "grade": score_to_grade(score * 100.0),
                }
            )

    return {
        "score": composite_score,
        "anti_pattern_penalty": penalty,
        "dimensions": dim_objects,
        "badge": badge_from_score(composite_score),
        "confidence_label": DEPTH_CONFIDENCE_LABELS.get(depth, DEPTH_CONFIDENCE_LABELS["quick"]),
    }


def _blend_layer_scores(static_scores: dict | None, judge_scores: dict | None) -> dict:
    """跨可用层混合维度分，权重再归一化；无任何层数据的维度记 -1.0 哨兵。"""
    blended = {}

    for dim in DIMENSION_WEIGHTS:
        blends = LAYER_BLENDS.get(dim, {"static": 1.0, "judge": 0.0, "monte_carlo": 0.0})

        available = {}
        if static_scores is not None and dim in static_scores:
            available["static"] = static_scores[dim]
        if judge_scores is not None and dim in judge_scores:
            available["judge"] = judge_scores[dim]

        if not available:
            blended[dim] = -1.0
            continue

        total_weight = sum(blends.get(layer, 0.0) for layer in available)
        if total_weight == 0.0:
            # 可用层的混合权重全为 0——等权平均
            blended[dim] = sum(available.values()) / len(available)
        else:
            blended[dim] = sum(
                available[layer] * blends.get(layer, 0.0) / total_weight for layer in available
            )

    return blended


def _map_static_to_dimensions(static_result: dict) -> dict:
    """静态子分映射到维度名。"""
    mapped = {}
    for sub_name, dim_name in STATIC_TO_DIMENSION.items():
        if sub_name in static_result.get("sub_scores", {}):
            value = static_result["sub_scores"][sub_name]
            if isinstance(value, (int, float)):
                mapped[dim_name] = float(value)
    return mapped


def merge_model_usage(layers: list) -> dict:
    """合并各层记录的按模型 token 用量（无模型调用的层不含该键）。"""
    merged = {}
    for layer in layers:
        usage = (layer.get("metadata") or {}).get("model_usage")
        if not isinstance(usage, dict):
            continue
        for model, tokens in usage.items():
            if isinstance(tokens, int):
                merged[model] = merged.get(model, 0) + tokens
    return merged
