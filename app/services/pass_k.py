"""pass^k 试验统计核心（M2 QE2-3，pass^k 与统计严谨性）

语义对齐 τ-bench pass^k：同一输入行重复 k 次 trial（重跑被测模型 +
judge/校验），从「trial 是否通过」的二值序列得出两个口径——

- ``pass_at_1``：单次 trial 通过率的期望（k 次 trial 通过数的均值），
  即「随机抽一次能通过」的概率估计；
- ``pass_pow_k``：k 次 trial **全部通过**的行占比（行级为 0/1），
  即「连跑 k 次不翻车」的能力下界——对截断 trace/偶发失败更敏感。

行级多 trial 的类型接线（同口径聚合，plans §2.3）：

- judge：``evaluation_tasks.trials``（2-5），执行器行内跑 k 次合并为一行；
- success_rate / consistency：既有 ``repeat_count`` 即 k，trial 通过=调用成功；
- latency：既有 ``latency_config.repeat_count`` 即 k（同一样本展开重复），
  trial 通过=单次耗时 ≤ 阈值。

trial「通过」的判定按类型优先级：配置终态校验器时以
``verification_result.status == pass`` 为准（确定性口径，M2 验收的
「校验器 + pass^k 全链路」）；未配置时退化为「trial 产出有效裁判分」。
分数标准差/标准误（score_std/score_sem）为样本标准差（n-1 分母）与
``std / sqrt(n)``，供「均分 ± 波动」的展示与显著性输入。
"""
import math
from typing import Any, Dict, List, Optional, Sequence

# 方案口径：trials 可选 2-5（1 = 关闭多 trial，行为与旧版一致）
TRIALS_MIN = 2
TRIALS_MAX = 5


def normalize_trials(value) -> int:
    """归一化 trials 配置为 1..5 的整数（None/非法值 → 1，即不开启）

    与 trajectory/verifier 校验的宽容口径一致：存量任务无该字段、
    手写 API 传坏值都不阻断任务创建，只按未配置处理。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1
    trials = int(value)
    if trials < TRIALS_MIN:
        return 1
    return min(trials, TRIALS_MAX)


def row_trials(task) -> int:
    """从任务对象读行级 trial 数（M2 QE2-3：仅 judge 类型生效）

    success_rate/consistency/latency 的多次重复走各自既有的
    repeat_count 通道，不经本字段（避免两套 k 互相打架）。
    """
    if getattr(task, "evaluation_type", "") != "judge":
        return 1
    return normalize_trials(getattr(task, "trials", None))


def trial_pass_stats(passes: Sequence[bool]) -> Dict[str, Any]:
    """trial 通过序列 → pass@1 / pass^k 统计（纯函数，手算可对照）

    Returns:
        {trials, pass_count, pass_at_1, pass_pow_k}；pass_at_1 四舍五入
        4 位小数（与平台 0-1 比率精度一致），pass_pow_k 为 0/1（行级）。
    """
    trials = len(passes)
    pass_count = sum(1 for p in passes if p)
    return {
        "trials": trials,
        "pass_count": pass_count,
        "pass_at_1": round(pass_count / trials, 4) if trials else 0.0,
        # 行级 pass^k：全过才是 1（τ-bench 语义的行级投影）
        "pass_pow_k": 1 if trials and pass_count == trials else 0,
    }


def score_stats(scores: Sequence[float]) -> Dict[str, float]:
    """trial 分数序列 → 均值/样本标准差/标准误（n-1 分母；n<2 时 std/sem=0）

    consistency/success_rate 的 trial 分数为 0/1 二值（trial 通过即 1），
    std/sem 描述「通过次数的波动」，同样可手算对照。
    """
    values = [float(s) for s in scores]
    n = len(values)
    mean = sum(values) / n if n else 0.0
    if n >= 2:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    sem = std / math.sqrt(n) if n else 0.0
    return {
        "score_mean": round(mean, 4),
        "score_std": round(std, 4),
        "score_sem": round(sem, 4),
    }


def aggregate_pass_k_rows(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """行级 pass^k 字段 → 任务级聚合（行携带 pass_at_1 才计入，legacy 兼容）

    输入行须带 metrics 内嵌或顶层 pass_at_1/pass_pow_k（两种形态都收，
    覆盖 consistency/success_rate 的 metrics 内嵌与 judge 合并行顶层）。

    Returns:
        {rows, average_pass_at_1, average_pass_pow_k, all_pass_rows}；
        无任何行携带该字段时返回 None（聚合不写 pass_k 键，行为与旧版一致）。
    """
    average_pass_at_1 = 0.0
    average_pass_pow_k = 0.0
    all_pass_rows = 0
    collected: List[Dict] = []
    for row in rows:
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        pass_at_1 = row.get("pass_at_1", metrics.get("pass_at_1"))
        pass_pow_k = row.get("pass_pow_k", metrics.get("pass_pow_k"))
        if pass_at_1 is None or pass_pow_k is None:
            continue
        collected.append({"pass_at_1": float(pass_at_1), "pass_pow_k": float(pass_pow_k)})
    if not collected:
        return None
    average_pass_at_1 = sum(c["pass_at_1"] for c in collected) / len(collected)
    average_pass_pow_k = sum(c["pass_pow_k"] for c in collected) / len(collected)
    all_pass_rows = sum(1 for c in collected if c["pass_pow_k"] >= 1)
    return {
        "rows": len(collected),
        "average_pass_at_1": round(average_pass_at_1, 4),
        "average_pass_pow_k": round(average_pass_pow_k, 4),
        "all_pass_rows": all_pass_rows,
    }
