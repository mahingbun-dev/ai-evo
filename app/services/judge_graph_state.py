"""
Judge LangGraph Agent — 状态定义

定义裁判 Agent 在 LangGraph 中流转的完整状态结构。
所有用户配置作为结构化字段传入，节点按需读取。
"""
import logging
from typing import TypedDict, List, Dict, Any, Annotated
import operator

logger = logging.getLogger(__name__)


class DimensionDependency(TypedDict):
    """维度依赖规则 — 当 source_dimension 的评分结果满足 condition 条件时，该维度的分数将被 override_value 覆盖"""
    source_dimension: str        # 被依赖的源维度名称
    condition: str               # 条件运算符: "eq", "ne", "gt", "gte", "lt", "lte"
    value: float                 # 比较基准值
    override_value: float        # 条件成立时的覆盖分数


class JudgeAgentState(TypedDict):
    """LangGraph 裁判 Agent 的状态"""

    # ── 1. 用户配置 (from task, 只读) ──
    task_id: str
    scene_desc: str
    evaluation_metrics: List[Dict]
    dimension_dependencies: Annotated[Dict[str, List[DimensionDependency]], operator.or_]
    max_score: int
    score_details: str
    scoring_steps: List[str]
    judge_instruction: str
    judge_model_config: Dict[str, Any]     # 用于 LLM 调用的模型配置

    # ── 2. 运行时数据 (from pipeline, 只读) ──
    resolved_targets_content: str
    user_variables: List[Dict]

    # ── 3. 中间结果 (节点逐项填充) ──
    analysis: str
    # Dict 合并归约器: 多个节点各自写入不同维度，自动合并
    dimension_results: Annotated[Dict[str, Dict], operator.or_]
    scored_dimensions: Annotated[List[str], operator.add]

    # ── 4. 最终输出 ──
    final_score: float
    final_feedback: str
    final_dimension_scores: Dict[str, float]
    # 分维度评分理由（aggregate_scores 从 dimension_results 组装，仅收录成功
    # 评分的维度；缺失 reasoning 时置空字符串），由 format_output 写入输出顶层
    final_dimension_feedbacks: Dict[str, str]
    # W2-1 失败显式标记：任一维度解析失败时该裁判整体标记失败，
    # 由 format_output 写入 raw_output，行级聚合据此剔除该裁判
    final_judge_failed: bool
    final_fail_reason: str
    raw_output: str


def build_state_input(
    task,
    resolved_targets_content: str = "",
    user_variables: List[Dict] = None,
    overrides: Dict[str, Any] = None,
) -> JudgeAgentState:
    """
    从 task 对象和运行时数据构建初始状态。

    Args:
        task: 评测任务对象 (SQLAlchemy model)
        resolved_targets_content: evaluation_targets 解析后的文本内容
        user_variables: 用户变量列表 [{"name": "用户指令", "value": "..."}]
        overrides: 覆盖件（可选）{score_details, evaluation_metrics}，由执行链路
            解析 @ 引用标记后传入，优先于 task 同名字段（契约 v3 §三）

    Returns:
        JudgeAgentState 初始值
    """
    # 从 task 获取 judge_model 配置
    judge_config = _load_judge_model_config(task)

    # overrides 优先：score_details / evaluation_metrics 用覆盖件替代 task 同名字段
    overrides = overrides or {}
    metrics = overrides.get("evaluation_metrics")
    if metrics is None:
        metrics = getattr(task, "evaluation_metrics", None) or []
    score_details = overrides.get("score_details")
    if score_details is None:
        score_details = getattr(task, "score_details", None) or ""

    deps = _extract_dependencies(metrics)

    return {
        "task_id": str(task.id),
        "scene_desc": getattr(task, "scene_desc", "") or "",
        "evaluation_metrics": metrics,
        "dimension_dependencies": deps,
        "max_score": getattr(task, "max_score", 10) or 10,
        "score_details": score_details,
        # 评分步骤不再读 task.steps，改从（overrides 优先的）evaluation_metrics
        # 顺序派生：维度顺序即步骤（契约 v3 §四）
        "scoring_steps": derive_scoring_steps(metrics),
        "judge_instruction": getattr(task, "judge_prompt", "") or "",
        "judge_model_config": judge_config,
        "resolved_targets_content": resolved_targets_content or "",
        "user_variables": user_variables or [],
        # 空初始值，由节点填充
        "analysis": "",
        "dimension_results": {},
        "scored_dimensions": [],
        "final_score": 0.0,
        "final_feedback": "",
        "final_dimension_scores": {},
        "final_dimension_feedbacks": {},
        "final_judge_failed": False,
        "final_fail_reason": "",
        "raw_output": "",
    }


def derive_scoring_steps(metrics: List[Dict]) -> List[str]:
    """
    从 evaluation_metrics 顺序派生评分步骤列表。

    每个维度产出一条 "{序号}. {维度名}：{维度定义}"，序号取维度在列表中的
    位置（1 起），与单维度评分节点按名称定位的步骤序号保持一致。

    Args:
        metrics: 评测维度配置列表

    Returns:
        评分步骤文本列表，无维度时为空列表
    """
    steps = []
    for i, metric in enumerate(metrics or [], 1):
        if not isinstance(metric, dict):
            continue
        name = metric.get("name", "")
        if not name:
            continue
        description = metric.get("description", "") or ""
        steps.append(f"{i}. {name}：{description}")
    return steps


def _extract_dependencies(metrics: List[Dict]) -> Dict[str, List[DimensionDependency]]:
    """
    从 evaluation_metrics 中提取维度依赖配置。

    每个 metric 可选地包含 dependencies 列表，格式为:
        [{"source_dimension": "...", "condition": "eq", "value": 0, "override_value": 0}]

    返回以 metric name 为 key 的字典，方便节点按维度名称快速查询。

    Args:
        metrics: evaluation_metrics 列表

    Returns:
        {metric_name: [{source_dimension, condition, value, override_value}, ...]}
    """
    result: Dict[str, List[DimensionDependency]] = {}
    for metric in (metrics or []):
        metric_name = metric.get("name", "")
        if not metric_name:
            continue
        deps = metric.get("dependencies", [])
        if not deps:
            continue
        result[metric_name] = [
            {
                "source_dimension": d.get("source_dimension", ""),
                "condition": d.get("condition", ""),
                "value": d.get("value", 0.0),
                "override_value": d.get("override_value", 0.0),
            }
            for d in deps
        ]
    return result


def _load_judge_model_config(task) -> Dict[str, Any]:
    """从 task 中加载裁判模型配置（W4 快照优先，实时回退）

    优先级（由 config_snapshot.resolve_judge_call_config 统一实现，与执行
    记录归因摘要共用同一解析链）：
    1. task.judge_model_config 创建时快照（口径字段来自快照，凭据 api_key/
       auth_config 从实时 ModelConfig 解析——快照只存掩码，严禁明文）；
    2. 全局 Claude Code 配置（软退场：裁判模型统一由 CC 配置驱动）；
    3. task.judge_model 对应的实时 ModelConfig（历史任务可复跑）；
    4. consistency_config.judge_config（一致性评测历史兜底）。
    """
    from app.services.config_snapshot import resolve_judge_call_config

    judge_config, _source = resolve_judge_call_config(task)
    return judge_config
