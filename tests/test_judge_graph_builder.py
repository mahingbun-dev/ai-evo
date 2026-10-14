"""Tests for judge graph builder module"""
import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestJudgeGraphBuilder:
    """Test graph builder topology"""

    def test_build_graph_with_metrics(self):
        """Test graph builds with multiple dimension nodes"""
        from app.services.judge_graph_builder import build_judge_graph

        class MockTask:
            evaluation_metrics = [
                {"name": "准确性", "weight": "high", "description": "内容准确"},
                {"name": "流畅性", "weight": "medium", "description": "表达流畅"},
            ]

        graph = build_judge_graph(MockTask())
        assert graph is not None
        # Check nodes
        nodes = list(graph.nodes.keys())
        assert "analyze_output" in nodes
        assert "score_0" in nodes
        assert "score_1" in nodes
        assert "aggregate_scores" in nodes
        assert "format_output" in nodes

    def test_build_graph_no_metrics(self):
        """Test graph builds with no evaluation metrics"""
        from app.services.judge_graph_builder import build_judge_graph

        class MockTask:
            evaluation_metrics = []

        graph = build_judge_graph(MockTask())
        assert graph is not None
        nodes = list(graph.nodes.keys())
        assert "analyze_output" in nodes
        assert "aggregate_scores" in nodes
        assert "format_output" in nodes
        # No score nodes
        assert not any(n.startswith("score_") for n in nodes)
