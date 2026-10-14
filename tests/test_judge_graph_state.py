"""Tests for judge graph state module"""
import pytest
from app.services.judge_graph_state import JudgeAgentState, DimensionDependency

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestBuildStateInput:
    """Test build_state_input with mock task"""

    def test_build_state_input_basic(self):
        """Test basic state building with minimal task"""
        class MockTask:
            id = "test-123"
            scene_desc = "测试场景"
            evaluation_metrics = [
                {"name": "准确性", "weight": "high", "description": "内容准确"}
            ]
            max_score = 10
            score_details = ""
            steps = []
            judge_prompt = "请进行评测"
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        from app.services.judge_graph_state import build_state_input

        state = build_state_input(
            task=MockTask(),
            resolved_targets_content="测试内容",
            user_variables=[{"name": "用户指令", "value": "测试"}],
        )

        assert state["task_id"] == "test-123"
        assert state["scene_desc"] == "测试场景"
        assert len(state["evaluation_metrics"]) == 1
        assert state["max_score"] == 10
        assert state["resolved_targets_content"] == "测试内容"
        assert len(state["user_variables"]) == 1
        # 没有 dependencies 的 metric -> dimension_dependencies 应为空
        assert state["dimension_dependencies"] == {}
        # scoring_steps 从 evaluation_metrics 顺序派生（契约 v3 §四），不读 task.steps
        assert state["scoring_steps"] == ["1. 准确性：内容准确"]
        # Check empty initial values
        assert state["analysis"] == ""
        assert state["dimension_results"] == {}
        assert state["scored_dimensions"] == []
        assert state["final_score"] == 0.0
        assert state["final_feedback"] == ""

    def test_build_state_input_empty_task(self):
        """Test state building with task that has minimal attributes"""
        class EmptyTask:
            id = "empty-999"

        from app.services.judge_graph_state import build_state_input

        state = build_state_input(task=EmptyTask())

        assert state["task_id"] != ""
        assert state["scene_desc"] == ""
        assert state["evaluation_metrics"] == []
        assert state["max_score"] == 10
        assert state["score_details"] == ""
        assert state["judge_instruction"] == ""
        # 空 metrics -> 派生的 scoring_steps 也为空
        assert state["scoring_steps"] == []
        # 空 metrics -> dimension_dependencies 应为空
        assert state["dimension_dependencies"] == {}

    def test_build_state_input_with_dependencies(self):
        """Test build_state_input: metric with dependencies correctly fills dimension_dependencies"""
        class MockTask:
            id = "dep-456"
            scene_desc = "带依赖的评测"
            evaluation_metrics = [
                {
                    "name": "代码质量",
                    "weight": "medium",
                    "description": "",
                    "dependencies": [
                        {"source_dimension": "功能完整性", "condition": "eq", "value": 0, "override_value": 0}
                    ],
                },
                {
                    "name": "性能",
                    "weight": "high",
                    "description": "性能评测",
                    "dependencies": [
                        {"source_dimension": "响应时间", "condition": "gt", "value": 10.0, "override_value": 5.0},
                        {"source_dimension": "资源占用", "condition": "lt", "value": 50.0, "override_value": 8.0},
                    ],
                },
                {
                    "name": "可维护性",
                    "weight": "low",
                    "description": "无依赖的维度",
                    # 没有 dependencies 字段
                },
            ]
            max_score = 100
            score_details = ""
            steps = []
            judge_prompt = "请评测"
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        from app.services.judge_graph_state import build_state_input

        state = build_state_input(task=MockTask())

        deps = state["dimension_dependencies"]
        assert "代码质量" in deps
        assert "性能" in deps
        # 没有 dependencies 的维度不应出现在结果中
        assert "可维护性" not in deps

        # 验证代码质量的依赖
        assert len(deps["代码质量"]) == 1
        code_quality_dep = deps["代码质量"][0]
        assert code_quality_dep["source_dimension"] == "功能完整性"
        assert code_quality_dep["condition"] == "eq"
        assert code_quality_dep["value"] == 0.0
        assert code_quality_dep["override_value"] == 0.0

        # 验证性能的依赖
        assert len(deps["性能"]) == 2
        perf_dep1 = deps["性能"][0]
        perf_dep2 = deps["性能"][1]
        assert perf_dep1["source_dimension"] == "响应时间"
        assert perf_dep1["condition"] == "gt"
        assert perf_dep1["value"] == 10.0
        assert perf_dep1["override_value"] == 5.0
        assert perf_dep2["source_dimension"] == "资源占用"
        assert perf_dep2["condition"] == "lt"
        assert perf_dep2["value"] == 50.0
        assert perf_dep2["override_value"] == 8.0

    def test_build_state_input_scoring_steps_derived_from_metrics_order(self):
        """scoring_steps 按 evaluation_metrics 顺序派生为 'N. 名称：描述'（task.steps 不再参与）"""
        class MockTask:
            id = "steps-789"
            scene_desc = ""
            evaluation_metrics = [
                {"name": "准确性", "weight": "高", "description": "内容准确"},
                {"name": "完整性", "weight": "中", "description": "覆盖完整"},
                {"name": "可读性", "weight": "低", "description": ""},
            ]
            max_score = 10
            score_details = ""
            steps = ["旧版步骤1", "旧版步骤2"]  # 旧字段，应被忽略
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        from app.services.judge_graph_state import build_state_input

        state = build_state_input(task=MockTask())

        assert state["scoring_steps"] == [
            "1. 准确性：内容准确",
            "2. 完整性：覆盖完整",
            "3. 可读性：",
        ]
        # 旧版 task.steps 不再进入 scoring_steps
        assert all("旧版步骤" not in s for s in state["scoring_steps"])

    def test_build_state_input_overrides_take_precedence(self):
        """overrides 的 score_details / evaluation_metrics 优先于 task 同名字段（契约 v3 §三）"""
        class MockTask:
            id = "ovr-001"
            scene_desc = "原始场景"
            evaluation_metrics = [
                {"name": "准确性", "weight": "高", "description": "原始维度"}
            ]
            max_score = 10
            score_details = "原始评分标准 {{ref:var:x}}"
            steps = ["旧步骤"]
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        from app.services.judge_graph_state import build_state_input

        overrides = {
            "score_details": "解析后的评分标准",
            "evaluation_metrics": [
                {"name": "准确性", "weight": "高", "description": "解析后维度"},
                {"name": "完整性", "weight": "中", "description": "新增维度"},
            ],
        }
        state = build_state_input(task=MockTask(), overrides=overrides)

        # evaluation_metrics / score_details 用 overrides 值
        assert state["evaluation_metrics"] == overrides["evaluation_metrics"]
        assert state["score_details"] == "解析后的评分标准"
        # scoring_steps 从 overrides 的 evaluation_metrics 派生
        assert state["scoring_steps"] == [
            "1. 准确性：解析后维度",
            "2. 完整性：新增维度",
        ]
        # 依赖也从 overrides 的 metrics 提取
        assert state["dimension_dependencies"] == {}

    def test_build_state_input_overrides_partial_falls_back_to_task(self):
        """overrides 只给单字段时，其余字段回退 task 同名字段"""
        class MockTask:
            id = "ovr-002"
            scene_desc = ""
            evaluation_metrics = [{"name": "准确性", "weight": "高", "description": "task维度"}]
            max_score = 10
            score_details = "task评分标准"
            steps = []
            judge_prompt = ""
            judge_model_config = {"provider": "openai", "model_type": "gpt-4"}
            judge_model = None

        from app.services.judge_graph_state import build_state_input

        # 只覆盖 score_details，evaluation_metrics 回退 task
        state = build_state_input(
            task=MockTask(),
            overrides={"score_details": "覆盖后的评分标准"},
        )
        assert state["score_details"] == "覆盖后的评分标准"
        assert state["evaluation_metrics"] == [{"name": "准确性", "weight": "高", "description": "task维度"}]
        assert state["scoring_steps"] == ["1. 准确性：task维度"]
