"""评测维度最高分与分数归一化的共享助手

维度最高分（evaluation_metrics[].max_score）由前端配置并随任务入库：
- 缺省 / 非法 / ≤0 时回退任务满分（与历史行为一致，存量任务不受影响）；
- 归一化口径：维度分先夹到 [0, 维度最高分]，再等比换算到任务满分标度后
  参与加权平均——保证「最高分 5 的维度打 5」与「最高分 10 的维度打 10」
  等价（都是满分表现），避免低量程维度在原始分加权平均中被稀释。

该模块只做纯计算，不依赖任何 app 内模块，供 prompt_builder /
judge_graph_nodes / judge_evaluation_engine / evaluation_executor /
claude_code_judge_agent 共用（无循环导入）。
"""
from typing import Any, Dict, Optional


def metric_max_score(metric: Optional[Dict[str, Any]], task_max_score) -> float:
    """读取单个维度的最高分，缺省/非法/≤0 回退任务满分"""
    task_max = float(task_max_score) if task_max_score else 10.0
    raw = (metric or {}).get("max_score")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return task_max
    if value <= 0:
        return task_max
    return value


def clamp_to_metric_max(score, metric: Optional[Dict[str, Any]], task_max_score) -> float:
    """把维度分夹到 [0, 该维度最高分] 区间，非法输入计 0 分"""
    dim_max = metric_max_score(metric, task_max_score)
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(value, dim_max))


def normalize_dimension_score(score, metric: Optional[Dict[str, Any]], task_max_score) -> float:
    """维度分 → 任务满分标度：先夹到维度量程，再等比换算

    维度最高分等于任务满分时为恒等变换（存量任务口径不变）。
    """
    task_max = float(task_max_score) if task_max_score else 10.0
    dim_max = metric_max_score(metric, task_max)
    value = clamp_to_metric_max(score, metric, task_max)
    return value / dim_max * task_max


def has_custom_dimension_max(evaluation_metrics, task_max_score) -> bool:
    """任务是否配置了「非任务满分」的维度最高分

    用于 CC 引擎行级总分重算的守卫：未配置时走原逻辑（裁判自报分），
    保证存量任务行为完全不变。
    """
    task_max = float(task_max_score) if task_max_score else 10.0
    for metric in evaluation_metrics or []:
        if not isinstance(metric, dict):
            continue
        raw = metric.get("max_score")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and abs(value - task_max) > 1e-9:
            return True
    return False


def format_score_range(max_value: float) -> str:
    """分值上限展示：整数去小数点（10.0→"10"，5.5→"5.5"），用于 prompt 文案"""
    try:
        return f"{float(max_value):g}"
    except (TypeError, ValueError):
        return "10"
