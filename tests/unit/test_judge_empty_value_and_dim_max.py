"""空值判定规则注入 + 维度最高分贯通 单元测试

覆盖两条修复主线：
1. P0-1 空值判定规则：裁判 prompt 区分「字段存在但值为空」与「字段缺失」；
   空字符串裁判变量显式渲染（值为空）而非丢弃。
2. P0-2 维度最高分（evaluation_metrics[].max_score）：注入两引擎 prompt、
   评分夹取、加权归一化、CC 引擎行级总分服务端重算守卫。
"""
import pytest
from unittest.mock import patch

from app.services.metric_scoring import format_score_range
from app.services.prompt_guard import EMPTY_VALUE_RULES
from app.services.prompt_builder import PromptBuilder
from app.services.judge_evaluation_engine import JudgeEvaluationEngine
from app.services.judge_graph_nodes import (
    _compute_weighted_scores,
    analyze_output_node,
    create_score_dimension_node,
)
from app.services.claude_code_judge_agent import ClaudeCodeJudgeAgent
from app.services.evaluation_executor import EvaluationExecutor

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# 维度配置样例：A 任务满分 10，B 自定义最高分 5（对应用户截图场景）
METRICS_WITH_CUSTOM_MAX = [
    {"name": "维度A", "weight": 1, "description": "A 描述", "max_score": 10},
    {"name": "维度B", "weight": 1, "description": "B 描述", "max_score": 5},
]


def _make_builder(metrics, max_score=10):
    return PromptBuilder.from_config({
        "evaluation_metrics": metrics,
        "max_score": max_score,
        "scene_desc": "测试场景",
    })


def _graph_state(metrics, **extra):
    state = {
        "evaluation_metrics": metrics,
        "scene_desc": "测试场景",
        "max_score": 10,
        "score_details": "",
        "scoring_steps": [],
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
    state.update(extra)
    return state


# ---------------- P0-1a / P0-2b: PromptBuilder ----------------


class TestPromptBuilderEmptyValueRules:
    """system prompt 注入空值判定规则"""

    def _build_system_prompt(self, metrics=None):
        return _make_builder(metrics if metrics is not None else METRICS_WITH_CUSTOM_MAX)._build_system_prompt(
            user_input="问题", ref_answer="参考", model_output="输出",
            dataset_item={}, payload={},
        )

    def test_empty_value_rules_present(self):
        prompt = self._build_system_prompt()
        assert "数据判定规则" in prompt
        assert "字段存在但值为空" in prompt
        assert "字段缺失" in prompt
        assert "均为空时视为一致" in prompt

    def test_rules_before_metrics_section(self):
        """规则段位于评测维度段之前，先立口径再列维度"""
        prompt = self._build_system_prompt()
        assert prompt.index("数据判定规则") < prompt.index("## 评测维度")


class TestPromptBuilderDimMaxInjection:
    """维度最高分注入 system prompt 与输出格式"""

    def _build_system_prompt(self, metrics=None, max_score=10):
        return _make_builder(metrics if metrics is not None else METRICS_WITH_CUSTOM_MAX, max_score)._build_system_prompt(
            user_input="问题", ref_answer="参考", model_output="输出",
            dataset_item={}, payload={},
        )

    def test_dimension_header_contains_max(self):
        prompt = self._build_system_prompt()
        assert "### 第1步 维度A (权重: 1, 最高分: 10)" in prompt
        assert "### 第2步 维度B (权重: 1, 最高分: 5)" in prompt

    def test_missing_max_falls_back_to_task_max(self):
        metrics = [{"name": "维度A", "weight": 1, "description": ""}]
        prompt = self._build_system_prompt(metrics=metrics)
        assert "### 第1步 维度A (权重: 1, 最高分: 10)" in prompt

    def test_output_example_per_dim_range(self):
        """输出格式示例按各维度自己的最高分标注量程"""
        prompt = self._build_system_prompt()
        assert '"维度A": <该维度得分(0-10)>' in prompt
        assert '"维度B": <该维度得分(0-5)>' in prompt

    def test_output_reminder_per_dim_bound(self):
        prompt = self._build_system_prompt()
        assert "不得超过该维度的最高分" in prompt
        # 旧的全局一刀切表述不再出现
        assert "dimension_scores 中的每个维度分数必须是 0-10 之间的数字" not in prompt


class TestPromptBuilderEmptyVariableRendering:
    """空字符串裁判变量显式渲染（值为空），不再丢弃"""

    def test_list_format_empty_var_rendered(self):
        """列表格式：dataset_field 解析为空串 →（值为空）进 prompt"""
        config = {
            "judge_variable_config": [
                {"key": "expected", "name": "预期reason", "source": "dataset_field",
                 "source_config": {"field": "expected_reason"}, "prompt_type": "user"},
                {"key": "out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
            ],
        }
        builder = PromptBuilder.from_config(config)
        payload = {"expected_reason": ""}  # 预期字段本身为空
        prompt = builder.build_user_prompt_from_judge_vars(
            payload=payload, model_output='{"reason": ""}', chain_results=[]
        )
        # 变量值经 wrap_eval_data 包裹；空值以（值为空）显式占位
        assert "【预期reason】<eval_data>（值为空）</eval_data>" in prompt
        assert "【模型输出】" in prompt

    def test_dict_format_empty_var_rendered(self):
        config = {
            "judge_variable_config": {
                "expected": {"name": "预期reason", "source": "dataset_field",
                             "source_config": {"field": "expected_reason"}, "prompt_type": "user"},
            },
        }
        builder = PromptBuilder.from_config(config)
        prompt = builder.build_user_prompt_from_judge_vars(
            payload={"expected_reason": ""}, model_output="输出", chain_results=[]
        )
        assert "【预期reason】<eval_data>（值为空）</eval_data>" in prompt

    def test_non_empty_var_unchanged(self):
        config = {
            "judge_variable_config": [
                {"key": "expected", "name": "预期", "source": "dataset_field",
                 "source_config": {"field": "expected_reason"}, "prompt_type": "user"},
            ],
        }
        builder = PromptBuilder.from_config(config)
        prompt = builder.build_user_prompt_from_judge_vars(
            payload={"expected_reason": "冰箱制冷异常"}, model_output="输出", chain_results=[]
        )
        assert "【预期】<eval_data>冰箱制冷异常</eval_data>" in prompt
        assert "（值为空）" not in prompt

    def test_fallback_user_prompt_renders_empty_marker(self):
        """回退路径 _build_user_prompt 空串同样显式渲染（口径一致）"""
        config = {
            "judge_variable_config": [
                {"key": "expected", "name": "预期", "source": "dataset_field",
                 "source_config": {"field": "expected_reason"}, "prompt_type": "user"},
            ],
        }
        builder = PromptBuilder.from_config(config)
        prompt = builder._build_user_prompt(
            payload={"expected_reason": ""}, model_output="输出", chain_results=[]
        )
        assert "【预期】（值为空）" in prompt


# ---------------- P0-2e: calculate_weighted_score 归一化 ----------------


class TestCalculateWeightedScoreNormalization:
    """calculate_weighted_score 按维度最高分归一化"""

    def test_legacy_identity(self):
        """维度未配置最高分 → 与旧口径逐位一致"""
        metrics = [
            {"name": "A", "weight": 3},
            {"name": "B", "weight": 2},
        ]
        scores = {"A": {"score": 8}, "B": {"score": 6}}
        assert JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10) == 7.2

    def test_full_marks_low_range_not_diluted(self):
        """最高分 5 打 5 与最高分 10 打 10 等价（权重相同 → 满分 10）"""
        metrics = [
            {"name": "A", "weight": 1, "max_score": 10},
            {"name": "B", "weight": 1, "max_score": 5},
        ]
        scores = {"A": {"score": 10}, "B": {"score": 5}}
        assert JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10) == 10.0

    def test_mixed_weights(self):
        """A(满分10)打5 权重1 + B(满分5)打5 权重3 → (5 + 10*3)/4 = 8.75"""
        metrics = [
            {"name": "A", "weight": 1, "max_score": 10},
            {"name": "B", "weight": 3, "max_score": 5},
        ]
        scores = {"A": {"score": 5}, "B": {"score": 5}}
        assert JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10) == 8.75

    def test_over_range_score_clamped(self):
        """裁判超量程打分（满分5打10）→ 夹到满分表现，不产生超满分加权"""
        metrics = [{"name": "A", "weight": 1, "max_score": 5}]
        scores = {"A": {"score": 10}}
        assert JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10) == 10.0

    def test_flat_score_dict_supported(self):
        """CC 引擎形态 {维度名: 数字} 同样支持"""
        metrics = [
            {"name": "A", "weight": 1, "max_score": 10},
            {"name": "B", "weight": 1, "max_score": 5},
        ]
        scores = {"A": 10, "B": 5}
        assert JudgeEvaluationEngine.calculate_weighted_score(metrics, scores, 10) == 10.0


# ---------------- P0-1b / P0-2d: 图引擎节点 ----------------


class TestGraphAnalyzeNodeDimMax:
    """分析节点：维度摘要带最高分 + 空值规则"""

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_dim_summary_contains_max_and_rules(self, mock_caller):
        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": "<think>分析</think>"}

        analyze_output_node(_graph_state(METRICS_WITH_CUSTOM_MAX))

        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]
        assert "- 维度A (权重: 1, 最高分: 10): A 描述" in prompt
        assert "- 维度B (权重: 1, 最高分: 5): B 描述" in prompt
        assert "数据判定规则" in prompt


class TestGraphScoreNodeDimMax:
    """单维度评分节点：量程、夹取与空值规则"""

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_prompt_contains_dim_max_and_rules(self, mock_caller):
        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 4, "reasoning": "ok"}'}

        node = create_score_dimension_node(METRICS_WITH_CUSTOM_MAX[1])
        node(_graph_state(METRICS_WITH_CUSTOM_MAX))

        messages = mock_instance.call.call_args[0][0]
        prompt = messages[1]["content"]
        # 评分区间按维度最高分（而非任务满分 10）
        assert "(0-5分，分数不得超过该维度最高分)" in prompt
        assert "(0-10分" not in prompt
        # 无 score_criteria 且无全局 score_details 时的回退标准也用维度最高分
        assert "0-5分，越高越好" in prompt
        # 空值判定规则注入
        assert "字段存在但值为空" in prompt

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_clamped_to_dim_max(self, mock_caller):
        """裁判违反指令给 10（维度最高分 5）→ 夹到 5"""
        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 10, "reasoning": "满分"}'}

        node = create_score_dimension_node(METRICS_WITH_CUSTOM_MAX[1])
        result = node(_graph_state(METRICS_WITH_CUSTOM_MAX))
        assert result["dimension_results"]["维度B"]["score"] == 5.0

    @patch("app.services.judge_graph_nodes.ModelCallerSync")
    def test_score_within_range_unchanged(self, mock_caller):
        mock_instance = mock_caller.return_value
        mock_instance.call.return_value = {"success": True, "content": '{"score": 3, "reasoning": "ok"}'}

        node = create_score_dimension_node(METRICS_WITH_CUSTOM_MAX[1])
        result = node(_graph_state(METRICS_WITH_CUSTOM_MAX))
        assert result["dimension_results"]["维度B"]["score"] == 3.0


class TestGraphAggregationNormalization:
    """图引擎聚合：归一化后加权，反馈仍展示原始分"""

    def test_full_marks_low_range(self):
        metrics = [
            {"name": "A", "weight": 1, "max_score": 10},
            {"name": "B", "weight": 1, "max_score": 5},
        ]
        dim_results = {
            "A": {"score": 10.0, "reasoning": "A 理由"},
            "B": {"score": 5.0, "reasoning": "B 理由"},
        }
        total_weight, weighted_sum, feedback_parts = _compute_weighted_scores(metrics, dim_results, 10)
        assert total_weight == 2.0
        assert weighted_sum == pytest.approx(20.0)  # (10 + 10) * 1
        assert "【B】5.0分 - B 理由" in feedback_parts  # 反馈仍为原始分

    def test_legacy_identity(self):
        metrics = [{"name": "A", "weight": 3}, {"name": "B", "weight": 2}]
        dim_results = {"A": {"score": 8}, "B": {"score": 6}}
        _, weighted_sum, _ = _compute_weighted_scores(metrics, dim_results, 10)
        assert weighted_sum == pytest.approx(8 * 3 + 6 * 2)


# ---------------- P0-2c: CC 输出协议 ----------------


class TestCCProtocolDimSpecs:
    """CC 输出协议维度清单带各自最高分"""

    def _task(self):
        return type("Task", (), {
            "id": "task_x",
            "evaluation_metrics": METRICS_WITH_CUSTOM_MAX,
            "max_score": 10,
        })()

    def test_with_session_file(self):
        agent = ClaudeCodeJudgeAgent(self._task())
        prompt = agent._build_final_prompt("用户提示", "/tmp/session.md")
        assert "参与评分的维度及各自最高分：维度A(最高分10)、维度B(最高分5)" in prompt
        assert "每个维度的得分不得超过该维度自己的最高分" in prompt

    def test_without_session_file(self):
        agent = ClaudeCodeJudgeAgent(self._task())
        prompt = agent._build_final_prompt("用户提示", None)
        assert "必须评分的维度及各自最高分：维度A(最高分10)、维度B(最高分5)" in prompt

    def test_missing_max_falls_back(self):
        task = type("Task", (), {
            "id": "task_x",
            "evaluation_metrics": [{"name": "维度A", "weight": 1}],
            "max_score": 10,
        })()
        agent = ClaudeCodeJudgeAgent(task)
        prompt = agent._build_final_prompt("用户提示", None)
        assert "维度A(最高分10)" in prompt


# ---------------- P0-2f: CC 行级总分重算守卫 ----------------


class TestRenormalizeCCJudgeScores:
    """_renormalize_cc_judge_scores 守卫与重算"""

    def test_guard_off_unchanged(self):
        """未配置非默认维度最高分 → 裁判自报分原样保留"""
        metrics = [{"name": "A", "weight": 1, "max_score": 10}]
        results = [{"score": 7.5, "dimension_scores": {"A": 7}}]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, metrics, 10)
        assert out[0]["score"] == 7.5
        assert "judge_reported_score" not in out[0]

    def test_guard_on_recomputes(self):
        """裁判自报 7.5（被低量程维度稀释）→ 归一化重算为满分 10"""
        results = [{
            "score": 7.5,
            "dimension_scores": {"维度A": 10, "维度B": 5},
        }]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] == 10.0
        assert out[0]["judge_reported_score"] == 7.5

    def test_failed_judge_skipped(self):
        results = [{"score": None, "dimension_scores": {}, "judge_failed": True, "fail_reason": "llm_error"}]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] is None
        assert "judge_reported_score" not in out[0]

    def test_no_dimension_scores_skipped(self):
        """裁判只有总分没有维度分（解析兜底形态）→ 不重算"""
        results = [{"score": 8.0, "dimension_scores": {}}]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] == 8.0
        assert "judge_reported_score" not in out[0]

    def test_f_sentinel_dims_excluded_from_recompute(self):
        """验收 P1-1：本行不涉及的维度（F 哨兵经 normalize 剔除后维度缺失）
        不计权——重算只按实际评分维度微平均，自报 10 重算仍为 10"""
        results = [{
            "score": 10.0,
            # 真实管线形状：裁判输出 {维度A: 10, 维度B: "F"}，normalize 后 B 被剔除
            "dimension_scores": {"维度A": 10.0},
        }]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] == 10.0
        assert out[0]["judge_reported_score"] == 10.0

    def test_partial_involved_dims_micro_average(self):
        """部分维度涉及：涉及 A(满分10)打8 + B 不涉及 → 只按 A 重算得 8"""
        results = [{
            "score": 4.0,  # 自报分被全量微平均稀释的形态
            "dimension_scores": {"维度A": 8.0},
        }]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] == 8.0

    def test_all_dims_f_sentinel_keeps_reported(self):
        """全部维度都不涉及（dimension_scores 剔除后为空）→ 不重算"""
        results = [{"score": 9.0, "dimension_scores": {}}]
        out = EvaluationExecutor._renormalize_cc_judge_scores(results, METRICS_WITH_CUSTOM_MAX, 10)
        assert out[0]["score"] == 9.0
        assert "judge_reported_score" not in out[0]
