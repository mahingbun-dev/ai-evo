"""Tests for judge graph node implementations"""
import json
import pytest
from unittest.mock import patch


class TestScoreDimensionNode:
    """Test that the score dimension node factory works correctly"""

    def test_score_node_created(self):
        """Test node factory returns a callable function"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metric = {"name": "测试维度", "weight": "high", "description": "测试"}
        node = create_score_dimension_node(metric)
        assert callable(node)

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_node_prompt_contains_steps(self, mock_caller):
        """Test scoring steps, variables, and dependencies are injected into prompt"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metric = {"name": "测试维度", "weight": "high", "description": "测试"}
        node = create_score_dimension_node(metric)

        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 8, "reasoning": "评分通过"}'}

        state = {
            "evaluation_metrics": [metric],
            "scene_desc": "测试场景",
            "max_score": 10,
            "score_details": "",
            "scoring_steps": ["1. 先分析", "2. 再评分"],
            "judge_model_config": {"provider": "test"},
            "resolved_targets_content": "测试内容",
            "user_variables": [{"name": "预期", "value": "预期值"}],
            "analysis": "分析完成",
            "dimension_dependencies": {"测试维度": [{"source_dimension": "其他", "condition": "eq", "value": 0, "override_value": 0}]},
            "dimension_results": {},
            "scored_dimensions": [],
            "final_score": 0.0,
            "final_feedback": "",
            "final_dimension_scores": {},
            "raw_output": "",
            "task_id": "test",
            "judge_instruction": "",
        }

        result = node(state)

        # Capture prompt from ModelCallerSync.call's messages argument
        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]  # user message content

        # Verify all three injection sections appear in the prompt
        # 评分步骤段：维度顺序即步骤，仅提示当前维度在序列中的位置（契约 v3 §四）
        assert "[评分步骤]" in prompt
        assert "当前是第 1/1 步：测试维度" in prompt
        # 旧版完整步骤列表（来自 state["scoring_steps"]）不再注入
        assert "1. 先分析" not in prompt
        assert "2. 再评分" not in prompt

        assert "[参考数据]" in prompt
        assert "预期" in prompt
        assert "预期值" in prompt

        assert "[维度依赖规则]" in prompt
        assert "其他" in prompt

        # Verify the dimension name appears in the prompt
        assert "测试维度" in prompt

        # Verify result structure
        assert "dimension_results" in result
        assert "测试维度" in result["dimension_results"]
        assert result["dimension_results"]["测试维度"]["score"] == 8.0
        assert result["dimension_results"]["测试维度"]["reasoning"] == "评分通过"
        assert "scored_dimensions" in result
        assert "测试维度" in result["scored_dimensions"]

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_node_step_position_in_multi_metrics(self, mock_caller):
        """Test step position hint reflects the dimension's index in evaluation_metrics"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metrics = [
            {"name": "准确性", "weight": "high", "description": "内容准确"},
            {"name": "完整性", "weight": "medium", "description": "覆盖完整"},
            {"name": "可读性", "weight": "low", "description": "表达清晰"},
        ]
        node = create_score_dimension_node(metrics[1])

        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 6, "reasoning": "覆盖一般"}'}

        state = {
            "evaluation_metrics": metrics,
            "scene_desc": "",
            "max_score": 10,
            "score_details": "",
            "judge_model_config": {"provider": "test"},
            "resolved_targets_content": "测试内容",
            "user_variables": [],
            "analysis": "",
            "dimension_dependencies": {},
            "dimension_results": {},
            "scored_dimensions": [],
            "final_score": 0.0,
            "final_feedback": "",
            "final_dimension_scores": {},
            "raw_output": "",
            "task_id": "test",
            "judge_instruction": "",
        }

        node(state)

        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]

        # 第二个维度 → 第 2/3 步
        assert "当前是第 2/3 步：完整性" in prompt
        assert "当前是第 1/3 步" not in prompt
        assert "当前是第 3/3 步" not in prompt

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_node_returns_correct_dimension_name(self, mock_caller):
        """Test score node returns results keyed by the correct dimension name"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metric = {"name": "准确性", "weight": "high", "description": "内容准确性评测"}
        node = create_score_dimension_node(metric)

        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 9, "reasoning": "内容准确"}'}

        state = {
            "evaluation_metrics": [metric],
            "scene_desc": "",
            "max_score": 10,
            "score_details": "",
            "scoring_steps": [],
            "judge_model_config": {"provider": "test"},
            "resolved_targets_content": "测试内容",
            "user_variables": [],
            "analysis": "分析完成",
            "dimension_dependencies": {},
            "dimension_results": {},
            "scored_dimensions": [],
            "final_score": 0.0,
            "final_feedback": "",
            "final_dimension_scores": {},
            "raw_output": "",
            "task_id": "test",
            "judge_instruction": "",
        }

        result = node(state)
        assert "准确性" in result["dimension_results"]
        assert "测试维度" not in result["dimension_results"]
        assert result["dimension_results"]["准确性"]["score"] == 9.0
        assert "准确性" in result["scored_dimensions"]

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_node_handles_empty_injection_fields(self, mock_caller):
        """Test score node handles empty variables and no dependencies (steps derive from metrics)"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metric = {"name": "一致性", "weight": "high", "description": "测试"}
        node = create_score_dimension_node(metric)

        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 7, "reasoning": "一致性好"}'}

        state = {
            "evaluation_metrics": [metric],
            "scene_desc": "",
            "max_score": 10,
            "score_details": "",
            "judge_model_config": {"provider": "test"},
            "resolved_targets_content": "测试内容",
            "user_variables": [],            # empty variables — no [参考数据] injection
            "analysis": "",
            "dimension_dependencies": {},    # no deps — no [维度依赖规则] injection
            "dimension_results": {},
            "scored_dimensions": [],
            "final_score": 0.0,
            "final_feedback": "",
            "final_dimension_scores": {},
            "raw_output": "",
            "task_id": "test",
            "judge_instruction": "",
        }

        result = node(state)

        # Capture prompt to verify injection sections are absent
        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]
        # 评分步骤段由 evaluation_metrics 派生：有维度即注入位置提示
        assert "[评分步骤]" in prompt
        assert "当前是第 1/1 步：一致性" in prompt
        assert "[参考数据]" not in prompt
        assert "[维度依赖规则]" not in prompt

        # Should still produce valid results
        assert "dimension_results" in result
        assert "一致性" in result["dimension_results"]
        assert result["dimension_results"]["一致性"]["score"] == 7.0
        assert "scored_dimensions" in result
        assert "一致性" in result["scored_dimensions"]

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_node_step_hint_absent_when_dimension_not_in_metrics(self, mock_caller):
        """Test step hint is omitted when the dimension is missing from evaluation_metrics"""
        from app.services.judge_graph_nodes import create_score_dimension_node
        metric = {"name": "孤立维度", "weight": "high", "description": "不在 state 序列中"}
        node = create_score_dimension_node(metric)

        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 5, "reasoning": "无法定位"}'}

        state = {
            "evaluation_metrics": [{"name": "准确性", "weight": "high", "description": ""}],
            "scene_desc": "",
            "max_score": 10,
            "score_details": "",
            "judge_model_config": {"provider": "test"},
            "resolved_targets_content": "测试内容",
            "user_variables": [],
            "analysis": "",
            "dimension_dependencies": {},
            "dimension_results": {},
            "scored_dimensions": [],
            "final_score": 0.0,
            "final_feedback": "",
            "final_dimension_scores": {},
            "raw_output": "",
            "task_id": "test",
            "judge_instruction": "",
        }

        node(state)

        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]
        # 维度不在序列中 → 定位不到步骤位置，整段不注入
        assert "[评分步骤]" not in prompt


class TestAnalyzeOutputNode:
    """Test analyze output node"""

    def test_analyze_output_node_imported(self):
        """Test analyze_output_node is importable"""
        from app.services.judge_graph_nodes import analyze_output_node
        assert callable(analyze_output_node)


class TestAggregateScoresNode:
    """Test aggregate scores node"""

    def test_aggregate_scores_node_imported(self):
        """Test aggregate_scores_node is importable"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        assert callable(aggregate_scores_node)

    def test_aggregate_scores_empty(self):
        """Test aggregate with no metrics or results"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [],
            "dimension_results": {},
            "max_score": 10,
        }
        result = aggregate_scores_node(state)
        assert result["final_score"] == 0.0
        assert "没有有效的评分结果" in result["final_feedback"]

    def test_aggregate_scores_basic(self):
        """Test aggregate with single metric"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "准确性", "weight": "high"}
            ],
            "dimension_results": {
                "准确性": {"score": 9.0, "reasoning": "正确"}
            },
            "max_score": 10,
        }
        result = aggregate_scores_node(state)
        assert result["final_score"] == 9.0
        assert "准确性" in result["final_feedback"]

    def test_aggregate_scores_weighted(self):
        """Test weighted aggregation with multiple metrics"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "准确性", "weight": 2},
                {"name": "完整性", "weight": 1},
            ],
            "dimension_results": {
                "准确性": {"score": 8.0, "reasoning": "较好"},
                "完整性": {"score": 6.0, "reasoning": "一般"},
            },
            "max_score": 10,
        }
        result = aggregate_scores_node(state)
        # (8*2 + 6*1) / (2+1) = 22/3 ≈ 7.33
        assert result["final_score"] == pytest.approx(7.33, 0.01)
        assert "准确性" in result["final_feedback"]
        assert "完整性" in result["final_feedback"]
        assert "8.0分" in result["final_feedback"]
        assert "6.0分" in result["final_feedback"]

    def test_aggregate_scores_zero_weight(self):
        """Test aggregate with zero total weight"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "准确性", "weight": 0},
            ],
            "dimension_results": {
                "准确性": {"score": 8.0, "reasoning": "较好"},
            },
            "max_score": 10,
        }
        result = aggregate_scores_node(state)
        assert result["final_score"] == 0.0
        assert "权重之和为 0" in result["final_feedback"]

    def test_aggregate_dependency_override_eq(self):
        """Test dependency override: if source_dim eq 0, override dependent to 0"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "A", "weight": 1},
                {"name": "B", "weight": 1},
            ],
            "dimension_results": {
                "A": {"score": 0.0, "reasoning": "低分"},
                "B": {"score": 8.0, "reasoning": "高分"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "eq", "value": 0, "override_value": 0}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 0.0
        assert result["final_score"] == 0.0

    def test_aggregate_dependency_no_override(self):
        """Test dependency NOT triggered when condition not met"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "A", "weight": 1},
                {"name": "B", "weight": 1},
            ],
            "dimension_results": {
                "A": {"score": 5.0, "reasoning": "中等"},
                "B": {"score": 8.0, "reasoning": "高分"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "eq", "value": 0, "override_value": 0}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 8.0

    def test_aggregate_dependency_override_ne(self):
        """Test dependency: if source_dim ne 0, override dependent to 0"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [
                {"name": "A", "weight": 1},
                {"name": "B", "weight": 1},
            ],
            "dimension_results": {
                "A": {"score": 5.0, "reasoning": "非零"},
                "B": {"score": 8.0, "reasoning": "高分"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "ne", "value": 0, "override_value": 0}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 0.0
        assert result["final_score"] == 2.5  # A=5, B=0 → avg=2.5

    def test_aggregate_dependency_override_lt(self):
        """Test dependency: if source_dim lt 3, override dependent"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
            "dimension_results": {
                "A": {"score": 2.0, "reasoning": "低"},
                "B": {"score": 8.0, "reasoning": "高分"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "lt", "value": 3, "override_value": 0}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 0.0

    def test_aggregate_dependency_override_gt(self):
        """Test dependency: if source_dim gt 7, override dependent"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
            "dimension_results": {
                "A": {"score": 8.0, "reasoning": "高"},
                "B": {"score": 5.0, "reasoning": "中等"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "gt", "value": 7, "override_value": 1}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 1.0

    def test_aggregate_dependency_override_lte(self):
        """Test dependency: if source_dim lte 2, override dependent"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
            "dimension_results": {
                "A": {"score": 2.0, "reasoning": "边界"},
                "B": {"score": 8.0, "reasoning": "高分"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "lte", "value": 2, "override_value": 0}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 0.0

    def test_aggregate_dependency_override_gte(self):
        """Test dependency: if source_dim gte 9, override dependent"""
        from app.services.judge_graph_nodes import aggregate_scores_node
        state = {
            "evaluation_metrics": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
            "dimension_results": {
                "A": {"score": 9.0, "reasoning": "优秀"},
                "B": {"score": 5.0, "reasoning": "中等"},
            },
            "max_score": 10,
            "dimension_dependencies": {
                "B": [{"source_dimension": "A", "condition": "gte", "value": 9, "override_value": 1}],
            },
        }
        result = aggregate_scores_node(state)
        assert result["final_dimension_scores"]["B"] == 1.0


class TestFormatOutputNode:
    """Test format output node"""

    def test_format_output_node_imported(self):
        """Test format_output_node is importable"""
        from app.services.judge_graph_nodes import format_output_node
        assert callable(format_output_node)

    def test_format_output_node_produces_json(self):
        """Test format_output_node returns valid JSON string"""
        from app.services.judge_graph_nodes import format_output_node
        state = {
            "final_score": 8.5,
            "final_feedback": "测试反馈",
            "final_dimension_scores": {"准确性": 9.0, "完整性": 8.0},
            "dimension_results": {"准确性": {"score": 9.0}, "完整性": {"score": 8.0}},
            "analysis": "分析内容",
        }
        result = format_output_node(state)
        raw = result["raw_output"]
        parsed = json.loads(raw)
        assert parsed["score"] == 8.5
        assert parsed["feedback"] == "测试反馈"
        assert "准确性" in parsed["dimension_scores"]
        assert parsed["dimension_scores"]["准确性"] == 9.0
        assert "judge_details" in parsed
