"""
Integration tests for judge graph: end-to-end flow with mocked LLM

Tests the full graph execution pipeline:
  analyze_output -> score_0 -> score_1 -> ... -> aggregate_scores -> format_output -> END

All LLM calls are mocked via ModelCallerSync to avoid real API dependencies.
"""
import json
import pytest
from unittest.mock import patch


class TestJudgeGraphIntegration:
    """Verify full pipeline: config -> state -> graph -> raw_output"""

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_full_config_flow(self, mock_caller):
        """Test full config produces correct raw_output structure with multi-dimension evaluation"""
        from app.services.judge_graph_builder import compile_judge_graph
        from app.services.judge_graph_state import build_state_input

        # Mock LLM responses sequentially: 1 analyze + 2 score calls
        mock_instance = mock_caller.return_value
        call_counter = [0]

        def mock_call_side_effect(messages, config, runtime_vars):
            call_counter[0] += 1
            if call_counter[0] == 1:
                # Analyze node returns analysis
                return {"success": True, "content": "<think>分析完成</think>"}
            # Score nodes return scores
            return {"success": True, "content": '{"score": 8, "reasoning": "内容准确"}'}

        mock_instance.call.side_effect = mock_call_side_effect

        class MockTask:
            id = "integration-test"
            scene_desc = "测试评测场景"
            evaluation_metrics = [
                {
                    "name": "准确性",
                    "weight": "high",
                    "description": "内容是否准确",
                    "score_criteria": {
                        "优秀": {"min": 8, "max": 10, "desc": "准确"},
                        "良好": {"min": 5, "max": 7, "desc": "基本准确"},
                    },
                },
                {
                    "name": "完整性",
                    "weight": "medium",
                    "description": "覆盖是否完整",
                    "dependencies": [
                        {
                            "source_dimension": "准确性",
                            "condition": "eq",
                            "value": 0,
                            "override_value": 0,
                        }
                    ],
                },
            ]
            max_score = 10
            score_details = "9-10:优秀 7-8:良好 5-6:及格 0-4:不及格"
            steps = ["1. 分析内容", "2. 逐项评分"]
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        graph = compile_judge_graph(MockTask())
        state_input = build_state_input(
            task=MockTask(),
            resolved_targets_content="这是待评测的内容",
            user_variables=[{"name": "预期结果", "value": "预期值"}],
        )

        result = graph.invoke(state_input)
        output = json.loads(result["raw_output"])

        # Verify output structure
        assert "score" in output
        assert "feedback" in output
        assert "dimension_scores" in output
        assert "准确性" in output["dimension_scores"]
        assert "完整性" in output["dimension_scores"]
        assert "judge_details" in output

        # Verify judge_details structure
        details = output["judge_details"]
        assert "analysis" in details
        assert "dimension_details" in details
        assert "准确性" in details["dimension_details"]
        assert "完整性" in details["dimension_details"]

        # Verify scores are numeric
        assert isinstance(output["score"], float)
        assert isinstance(output["dimension_scores"]["准确性"], (int, float))

        # Verify all 3 LLM calls were made (1 analyze + 2 score nodes)
        assert call_counter[0] == 3

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_minimal_config_flow(self, mock_caller):
        """Test minimal config (empty scene_desc, metrics with no description, no variables) still
        produces valid output"""
        from app.services.judge_graph_builder import compile_judge_graph
        from app.services.judge_graph_state import build_state_input

        mock_instance = mock_caller.return_value
        mock_instance.call.side_effect = [
            {"success": True, "content": "<think>分析完成</think>"},
            {"success": True, "content": '{"score": 8, "reasoning": "内容准确"}'},
        ]

        class MinimalTask:
            id = "minimal-test"
            scene_desc = ""
            evaluation_metrics = [{"name": "准确性", "weight": "high", "description": ""}]
            max_score = 10
            score_details = ""
            steps = []
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        graph = compile_judge_graph(MinimalTask())
        state_input = build_state_input(
            task=MinimalTask(),
            resolved_targets_content="测试内容",
            user_variables=[],
        )

        result = graph.invoke(state_input)
        output = json.loads(result["raw_output"])

        # Verify output has valid structure
        assert isinstance(output["score"], float)
        assert output["score"] >= 0
        assert "准确性" in output["dimension_scores"]
        assert isinstance(output["feedback"], str)
        assert isinstance(output["judge_details"]["analysis"], str)

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_dependency_override_integration(self, mock_caller):
        """Test dependency override works end-to-end in graph pipeline

        Setup:
        - Dimension '准确性' (score_0) returns 0
        - Dimension '完整性' (score_1) returns 8
        - '完整性' has a dependency: if '准确性' == 0, override '完整性' to 0

        Expected:
        - raw_output.dimension_scores['完整性'] == 0 (overridden)
        - raw_output.score == 0.0 (both dimensions at 0)
        - judge_details.dimension_details shows override reasoning
        """
        from app.services.judge_graph_builder import compile_judge_graph
        from app.services.judge_graph_state import build_state_input

        mock_instance = mock_caller.return_value
        call_counter = [0]

        def mock_call_side_effect(messages, config, runtime_vars):
            call_counter[0] += 1
            if call_counter[0] == 1:
                # Analyze node
                return {"success": True, "content": "<think>分析完成</think>"}
            if call_counter[0] == 2:
                # score_0: '准确性' returns 0
                return {"success": True, "content": '{"score": 0, "reasoning": "内容不准确"}'}
            # score_1: '完整性' returns 8 (will be overridden to 0)
            return {"success": True, "content": '{"score": 8, "reasoning": "覆盖完整"}'}

        mock_instance.call.side_effect = mock_call_side_effect

        class DepTask:
            id = "dep-test"
            scene_desc = "依赖覆盖测试"
            evaluation_metrics = [
                {
                    "name": "准确性",
                    "weight": "high",
                    "description": "内容是否准确",
                },
                {
                    "name": "完整性",
                    "weight": "medium",
                    "description": "覆盖是否完整",
                    "dependencies": [
                        {
                            "source_dimension": "准确性",
                            "condition": "eq",
                            "value": 0,
                            "override_value": 0,
                        }
                    ],
                },
            ]
            max_score = 10
            score_details = ""
            steps = []
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        graph = compile_judge_graph(DepTask())
        state_input = build_state_input(
            task=DepTask(),
            resolved_targets_content="测试待评测内容",
            user_variables=[],
        )

        result = graph.invoke(state_input)
        output = json.loads(result["raw_output"])

        # Verify dependency override in dimension_scores
        assert output["dimension_scores"]["完整性"] == 0, (
            "'完整性' should be overridden to 0 because '准确性' scored 0"
        )
        assert output["dimension_scores"]["准确性"] == 0

        # With both dimensions at 0, final score should be 0
        assert output["score"] == 0.0

        # Verify judge_details shows the override reasoning
        details = output["judge_details"]
        assert "完整性" in details["dimension_details"]
        completeness_detail = details["dimension_details"]["完整性"]
        assert "覆盖" in completeness_detail.get("reasoning", ""), (
            "Override reasoning should contain '覆盖' indicating dependency override"
        )

        # Verify 3 LLM calls: analyze + score_0 + score_1
        assert call_counter[0] == 3


    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_dependency_no_override_when_condition_not_met(self, mock_caller):
        """Test dependency override is NOT triggered when condition is not met

        Setup:
        - Dimension '准确性' (score_0) returns 7 (not 0)
        - Dimension '完整性' (score_1) returns 8
        - '完整性' has a dependency: if '准确性' == 0, override '完整性' to 0

        Expected:
        - raw_output.dimension_scores['完整性'] == 8 (NOT overridden)
        - raw_output.score == (7*3 + 8*2) / 5 == 7.4
        """
        from app.services.judge_graph_builder import compile_judge_graph
        from app.services.judge_graph_state import build_state_input

        mock_instance = mock_caller.return_value
        call_counter = [0]

        def mock_call_side_effect(messages, config, runtime_vars):
            call_counter[0] += 1
            if call_counter[0] == 1:
                return {"success": True, "content": "<think>分析完成</think>"}
            if call_counter[0] == 2:
                return {"success": True, "content": '{"score": 7, "reasoning": "内容基本准确"}'}
            return {"success": True, "content": '{"score": 8, "reasoning": "覆盖完整"}'}

        mock_instance.call.side_effect = mock_call_side_effect

        class NoOverrideTask:
            id = "no-override-test"
            scene_desc = ""
            evaluation_metrics = [
                {
                    "name": "准确性",
                    "weight": "high",
                    "description": "",
                },
                {
                    "name": "完整性",
                    "weight": "medium",
                    "description": "",
                    "dependencies": [
                        {
                            "source_dimension": "准确性",
                            "condition": "eq",
                            "value": 0,
                            "override_value": 0,
                        }
                    ],
                },
            ]
            max_score = 10
            score_details = ""
            steps = []
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        graph = compile_judge_graph(NoOverrideTask())
        state_input = build_state_input(
            task=NoOverrideTask(),
            resolved_targets_content="测试内容",
            user_variables=[],
        )

        result = graph.invoke(state_input)
        output = json.loads(result["raw_output"])

        # '完整性' should NOT be overridden (准确率 = 7 != 0)
        assert output["dimension_scores"]["完整性"] == 8.0, (
            "'完整性' should remain at 8 since '准确性' (7) != 0"
        )
        assert output["dimension_scores"]["准确性"] == 7.0
        # (7*3 + 8*2) / 5 = 7.4
        assert output["score"] == pytest.approx(7.4, 0.01)
