"""
Judge LangGraph Agent — 图构建

根据评测任务动态构建 LangGraph 拓扑。
每个评测维度生成一个独立的评分节点。
"""
import logging
from langgraph.graph import StateGraph, END
from .judge_graph_state import JudgeAgentState
from .judge_graph_nodes import (
    analyze_output_node,
    create_score_dimension_node,
    aggregate_scores_node,
    format_output_node,
)

logger = logging.getLogger(__name__)


def build_judge_graph(task) -> StateGraph:
    """
    根据任务的 evaluation_metrics 动态构建 LangGraph。

    Graph 拓扑:
    analyze_output → score_0 → score_1 → ... → aggregate_scores → format_output → END

    Args:
        task: 评测任务对象

    Returns:
        未编译的 StateGraph
    """
    graph = StateGraph(JudgeAgentState)

    # 添加静态节点
    graph.add_node("analyze_output", analyze_output_node)
    graph.add_node("aggregate_scores", aggregate_scores_node)
    graph.add_node("format_output", format_output_node)

    # 为每个评测维度动态添加评分节点
    metrics = getattr(task, "evaluation_metrics", []) or []
    for i, metric in enumerate(metrics):
        node_name = f"score_{i}"
        score_node = create_score_dimension_node(metric)
        graph.add_node(node_name, score_node)

    # 连接边: 分析 → 逐维评分 → 聚合 → 格式化 → END
    graph.set_entry_point("analyze_output")

    prev_node = "analyze_output"
    for i in range(len(metrics)):
        node_name = f"score_{i}"
        graph.add_edge(prev_node, node_name)
        prev_node = node_name

    graph.add_edge(prev_node, "aggregate_scores")
    graph.add_edge("aggregate_scores", "format_output")
    graph.add_edge("format_output", END)

    return graph


def compile_judge_graph(task):
    """
    编译 LangGraph。

    Args:
        task: 评测任务对象

    Returns:
        编译后的 CompiledStateGraph
    """
    graph = build_judge_graph(task)
    compiled = graph.compile()
    logger.info(f"LangGraph 编译完成: {len(getattr(task, 'evaluation_metrics', []) or [])} 个维度节点")
    return compiled
