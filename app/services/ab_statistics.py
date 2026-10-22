"""A/B 显著性统计（M2 QE2-3，pass^k 与统计严谨性）

行配对（同输入）场景的两组统计：

- 连续值配对（每轮耗时/每行评分）：Wilcoxon 符号秩（主检验，对分布无
  正态假设）+ 配对 t 检验（正态性提示：n<30 或偏态时以 Wilcoxon 为准）
  + 效应量 Cliff's delta（非参数，|δ| 分级 negligible/small/medium/large，
  Romberg et al. 2016 阈值）+ 均值差的 95% 置信区间（t 分布）；
- 比例配对（每轮成功/失败 0-1）：McNemar 精确检验（不一致对 b/c 上的
  二项精确法），配对样本的卡方/z 检验在这里会忽略配对结构，不采用。

输出统一为 {p_value, effect..., conclusion, n, adequate}，结论语句形如
「B 显著优于 A（p=0.012，中效应），样本 n=120」，附「样本量是否足以
判定」提示（连续配对 n>=30、McNemar 不一致对 b+c>=10 视为充足）。

依赖 scipy（requirements.lock 已钉 1.18.0，arm64 wheel 齐备）。
纯函数无副作用：入参只读，异常输入（样本不足/全零差）不抛错，收敛为
带说明的结果 dict，调用方（WS/HTTP）直接透传给前端面板。
"""
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scipy import stats

# 显著性水平（结论语句与 CI 口径固定 95%）
ALPHA = 0.05

# Cliff's delta 效应量分级阈值（Romberg et al. 2016）
_DELTA_THRESHOLDS = ((0.147, "可忽略"), (0.33, "小"), (0.474, "中"))


def _delta_magnitude(abs_delta: float) -> str:
    for threshold, label in _DELTA_THRESHOLDS:
        if abs_delta < threshold:
            return label
    return "大"


def cliffs_delta(values_a: Sequence[float], values_b: Sequence[float]) -> Tuple[float, str]:
    """Cliff's delta（B 相对 A 的优势度）：#(b>a) - #(b<a) / (n_a*n_b)

    δ∈[-1,1]：正值 B 占优、负值 A 占优、0 完全重叠。纯手算可对照的
    双重循环定义（非参数，不假设分布形状）。
    """
    a = [float(v) for v in values_a]
    b = [float(v) for v in values_b]
    if not a or not b:
        return 0.0, "可忽略"
    greater = sum(1 for bv in b for av in a if bv > av)
    less = sum(1 for bv in b for av in a if bv < av)
    delta = (greater - less) / (len(a) * len(b))
    return round(delta, 4), _delta_magnitude(abs(delta))


def _pair_conclusion(direction_sign: float, p_value: float, effect_label: str, n: int) -> str:
    """配对检验结论语句：方向 × 显著性 × 效应量 × 样本量"""
    if p_value >= ALPHA:
        return f"A/B 无显著差异（p={p_value:.3f}，效应量：{effect_label}，样本 n={n}）"
    side = "B 显著优于 A" if direction_sign > 0 else "A 显著优于 B"
    return f"{side}（p={p_value:.3f}，{effect_label}效应），样本 n={n}"


def paired_comparison(
    values_a: Sequence[Optional[float]],
    values_b: Sequence[Optional[float]],
    metric_name: str = "指标",
    higher_is_better: bool = True,
) -> Dict[str, Any]:
    """Wilcoxon 符号秩 + 配对 t + Cliff's delta + 95% CI（行配对连续值）

    Args:
        values_a / values_b: 同输入配对的两侧数值（等长；任一侧缺失的
            位置整对剔除——两侧都没跑完的轮次不构成配对证据）
        metric_name: 展示用指标名（结论语句引用）
        higher_is_better: 指标方向——评分类 True（数值高=好），耗时类
            False（数值低=好）；只影响结论语句方向，不影响检验统计

    Returns:
        {method, n, mean_a, mean_b, mean_diff, diff_ci95, wilcoxon_p,
         paired_t_p, cliffs_delta, delta_magnitude, p_value, significant,
         adequate, conclusion, note}；样本不足（n<2）收敛为 insufficient。
    """
    pairs = [
        (float(a), float(b))
        for a, b in zip(values_a, values_b)
        if a is not None and b is not None
    ]
    n = len(pairs)
    base: Dict[str, Any] = {
        "method": "wilcoxon_paired",
        "metric_name": metric_name,
        "n": n,
        "adequate": n >= 30,
    }
    if n < 2:
        base.update({
            "p_value": None,
            "significant": False,
            "conclusion": f"配对样本不足（n={n}，至少 2 对同输入行），无法判定显著性",
            "note": "insufficient_samples",
        })
        return base

    diffs = [b - a for a, b in pairs]
    mean_a = sum(a for a, _ in pairs) / n
    mean_b = sum(b for _, b in pairs) / n
    mean_diff = sum(diffs) / n

    # 均值差 95% CI（t 分布）：mean_diff ± t_{0.975,n-1} * sd_diff/sqrt(n)
    diff_var = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    diff_sd = math.sqrt(diff_var)
    t_crit = float(stats.t.ppf(1 - (1 - 0.95) / 2, n - 1))
    ci_half = t_crit * diff_sd / math.sqrt(n)

    # Wilcoxon 符号秩（主检验）：全零差（两侧完全一致）时 p=1
    nonzero_diffs = [d for d in diffs if d != 0]
    if nonzero_diffs:
        wilcoxon_p = float(stats.wilcoxon(nonzero_diffs).pvalue)
        wilcoxon_note = ""
    else:
        wilcoxon_p = 1.0
        wilcoxon_note = "两侧逐对完全一致（差值全 0），Wilcoxon 无法区分"

    # 配对 t（正态性提示：非正态/小样本以 Wilcoxon 为准）
    paired_t_p = float(stats.ttest_rel([b for _, b in pairs], [a for a, _ in pairs]).pvalue)

    delta, magnitude = cliffs_delta(
        [a for a, _ in pairs], [b for _, b in pairs]
    )

    # 方向以均值差符号为准（与 CI 方向一致），按指标方向折算「优」的一侧；
    # p 值主口径取 Wilcoxon
    direction = 1 if mean_diff > 0 else (-1 if mean_diff < 0 else 0)
    if not higher_is_better:
        direction = -direction

    base.update({
        "mean_a": round(mean_a, 4),
        "mean_b": round(mean_b, 4),
        "mean_diff": round(mean_diff, 4),
        "diff_ci95": [round(mean_diff - ci_half, 4), round(mean_diff + ci_half, 4)],
        "wilcoxon_p": round(wilcoxon_p, 6),
        "paired_t_p": round(paired_t_p, 6),
        "cliffs_delta": delta,
        "delta_magnitude": magnitude,
        "p_value": round(wilcoxon_p, 6),
        "significant": wilcoxon_p < ALPHA,
        "conclusion": _pair_conclusion(direction, wilcoxon_p, magnitude, n),
        "note": wilcoxon_note or (
            "配对 t 假定差值近似正态，n<30 或偏态时以 Wilcoxon p 值为准"
            if n < 30 else ""
        ),
    })
    return base


def mcnemar_comparison(
    outcomes_a: Sequence[Optional[bool]],
    outcomes_b: Sequence[Optional[bool]],
    metric_name: str = "成功率",
) -> Dict[str, Any]:
    """McNemar 精确检验（配对比例：每轮/每行 成功-失败 二值）

    不一致对 b（A 过 B 败）与 c（A 败 B 过）上的双侧二项精确法
    （binomtest(min(b,c), b+c, 0.5)）；b+c=0（两侧逐对同胜负）p=1。

    Returns:
        {method, n, b, c, pass_a, pass_b, p_value, significant, adequate,
         conclusion, note}
    """
    pairs = [
        (bool(a), bool(b))
        for a, b in zip(outcomes_a, outcomes_b)
        if a is not None and b is not None
    ]
    n = len(pairs)
    base: Dict[str, Any] = {
        "method": "mcnemar_exact",
        "metric_name": metric_name,
        "n": n,
    }
    if n < 2:
        base.update({
            "p_value": None,
            "significant": False,
            "adequate": False,
            "conclusion": f"配对样本不足（n={n}，至少 2 对同输入行），无法判定显著性",
            "note": "insufficient_samples",
        })
        return base

    b_only = sum(1 for a, passed in pairs if a and not passed)   # A 过 B 败
    c_only = sum(1 for a, passed in pairs if not a and passed)   # A 败 B 过
    pass_a = sum(1 for a, _ in pairs if a)
    pass_b = sum(1 for _, passed in pairs if passed)

    discordant = b_only + c_only
    if discordant > 0:
        p_value = float(stats.binomtest(min(b_only, c_only), discordant, 0.5).pvalue)
        note = ""
    else:
        p_value = 1.0
        note = "两侧逐对同胜负（无不一致对），无法区分"

    direction = 1 if pass_b > pass_a else (-1 if pass_b < pass_a else 0)
    base.update({
        "b_a_pass_b_fail": b_only,
        "c_a_fail_b_pass": c_only,
        "pass_a": pass_a,
        "pass_b": pass_b,
        "discordant": discordant,
        "p_value": round(p_value, 6),
        "significant": p_value < ALPHA,
        # 判定力看不一致对数量（一致对不提供区分信息）
        "adequate": discordant >= 10,
        "conclusion": _pair_conclusion(direction, p_value, "比例差异", n),
        "note": note,
    })
    return base


def compute_ab_significance(rounds: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A/B 多轮记录 → 显著性统计总包（WS complete 与历史回放共用）

    Args:
        rounds: [{duration_a, duration_b, success_a, success_b}, ...]
            （同输入逐轮配对；duration 缺失以 None 占位）

    Returns:
        {rounds, duration_paired, success_mcnemar}；轮数 <2 返回 None
        （单轮无配对结构，不产生显著性面板数据）。
    """
    paired_rounds = [r for r in rounds if isinstance(r, dict)]
    if len(paired_rounds) < 2:
        return None
    return {
        "rounds": len(paired_rounds),
        "duration_paired": paired_comparison(
            [r.get("duration_a") for r in paired_rounds],
            [r.get("duration_b") for r in paired_rounds],
            metric_name="耗时(秒)",
            # 耗时越低越好：结论方向按 lower-is-better 折算
            higher_is_better=False,
        ),
        "success_mcnemar": mcnemar_comparison(
            [r.get("success_a") for r in paired_rounds],
            [r.get("success_b") for r in paired_rounds],
        ),
    }
