"""
DeepEval 指标注册表单元测试
"""
import os
from unittest.mock import patch

import pytest

from app.services.deepeval_metric_registry import (
    DeepEvalMetricRegistry,
    MetricNotFoundError,
)

# deepeval 指标在实例化时会尝试初始化 OpenAI 模型，
# 需要设置 dummy API key 以避免测试因缺少 key 而失败。
_DUMMY_API_KEY_ENV = {"OPENAI_API_KEY": "sk-test-dummy-key-for-testing"}


@pytest.fixture(autouse=False)
def mock_openai_api_key():
    """为 create_metric 测试提供 dummy OPENAI_API_KEY"""
    with patch.dict(os.environ, _DUMMY_API_KEY_ENV):
        yield


class TestGetMetricClass:
    """测试获取指标类"""

    # ---- Agent 评测指标 ----
    @pytest.mark.parametrize("metric_key,expected_class_name", [
        ("task_completion", "TaskCompletionMetric"),
        ("tool_correctness", "ToolCorrectnessMetric"),
        ("step_efficiency", "StepEfficiencyMetric"),
        ("argument_correctness", "ArgumentCorrectnessMetric"),
        ("plan_adherence", "PlanAdherenceMetric"),
        ("plan_quality", "PlanQualityMetric"),
    ])
    def test_get_agent_metric_class(self, metric_key: str, expected_class_name: str):
        """测试获取 Agent 评测指标类返回正确的类"""
        metric_class = DeepEvalMetricRegistry.get_metric_class(metric_key)
        assert metric_class.__name__ == expected_class_name

    # ---- 安全评测指标 ----
    @pytest.mark.parametrize("metric_key,expected_class_name", [
        ("bias", "BiasMetric"),
        ("toxicity", "ToxicityMetric"),
        ("pii_leakage", "PIILeakageMetric"),
        ("non_advice", "NonAdviceMetric"),
        ("misuse", "MisuseMetric"),
        ("role_violation", "RoleViolationMetric"),
    ])
    def test_get_safety_metric_class(self, metric_key: str, expected_class_name: str):
        """测试获取安全评测指标类返回正确的类"""
        metric_class = DeepEvalMetricRegistry.get_metric_class(metric_key)
        assert metric_class.__name__ == expected_class_name

    # ---- RAG 评测指标 ----
    @pytest.mark.parametrize("metric_key,expected_class_name", [
        ("answer_relevancy", "AnswerRelevancyMetric"),
        ("faithfulness", "FaithfulnessMetric"),
        ("hallucination", "HallucinationMetric"),
        ("contextual_precision", "ContextualPrecisionMetric"),
        ("contextual_recall", "ContextualRecallMetric"),
        ("contextual_relevancy", "ContextualRelevancyMetric"),
    ])
    def test_get_rag_metric_class(self, metric_key: str, expected_class_name: str):
        """测试获取 RAG 评测指标类返回正确的类"""
        metric_class = DeepEvalMetricRegistry.get_metric_class(metric_key)
        assert metric_class.__name__ == expected_class_name

    # ---- 对话评测指标 ----
    @pytest.mark.parametrize("metric_key,expected_class_name", [
        ("knowledge_retention", "KnowledgeRetentionMetric"),
        ("goal_accuracy", "GoalAccuracyMetric"),
    ])
    def test_get_conversation_metric_class(self, metric_key: str, expected_class_name: str):
        """测试获取对话评测指标类返回正确的类"""
        metric_class = DeepEvalMetricRegistry.get_metric_class(metric_key)
        assert metric_class.__name__ == expected_class_name

    # ---- 异常场景 ----
    def test_get_unknown_metric_raises_error(self):
        """测试获取未知指标类时抛出 MetricNotFoundError"""
        with pytest.raises(MetricNotFoundError) as exc_info:
            DeepEvalMetricRegistry.get_metric_class("unknown_metric")
        assert "unknown_metric" in str(exc_info.value)

    def test_get_unknown_metric_contains_available_keys(self):
        """测试异常信息中包含可用的指标 key 提示"""
        with pytest.raises(MetricNotFoundError) as exc_info:
            DeepEvalMetricRegistry.get_metric_class("nonexistent")
        error_msg = str(exc_info.value)
        assert "task_completion" in error_msg or "answer_relevancy" in error_msg


class TestListAvailableMetrics:
    """测试列出所有可用指标"""

    def test_list_returns_dict(self):
        """测试返回值为字典类型"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        assert isinstance(metrics, dict)

    def test_list_contains_all_categories(self):
        """测试返回的指标包含所有类别"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        # 检查各类别至少有一个指标
        categories = set(m["category"] for m in metrics.values())
        assert "agent" in categories
        assert "safety" in categories
        assert "rag" in categories
        assert "conversation" in categories

    def test_list_contains_expected_agent_metrics(self):
        """测试返回的指标包含预期的 Agent 指标"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        agent_keys = [k for k, v in metrics.items() if v["category"] == "agent"]
        assert "task_completion" in agent_keys
        assert "tool_correctness" in agent_keys
        assert "step_efficiency" in agent_keys
        assert "argument_correctness" in agent_keys
        assert "plan_adherence" in agent_keys
        assert "plan_quality" in agent_keys

    def test_list_contains_expected_safety_metrics(self):
        """测试返回的指标包含预期的安全指标"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        safety_keys = [k for k, v in metrics.items() if v["category"] == "safety"]
        assert "bias" in safety_keys
        assert "toxicity" in safety_keys
        assert "pii_leakage" in safety_keys
        assert "non_advice" in safety_keys
        assert "misuse" in safety_keys
        assert "role_violation" in safety_keys

    def test_list_contains_expected_rag_metrics(self):
        """测试返回的指标包含预期的 RAG 指标"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        rag_keys = [k for k, v in metrics.items() if v["category"] == "rag"]
        assert "answer_relevancy" in rag_keys
        assert "faithfulness" in rag_keys
        assert "hallucination" in rag_keys
        assert "contextual_precision" in rag_keys
        assert "contextual_recall" in rag_keys
        assert "contextual_relevancy" in rag_keys

    def test_list_contains_expected_conversation_metrics(self):
        """测试返回的指标包含预期的对话指标"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        conv_keys = [k for k, v in metrics.items() if v["category"] == "conversation"]
        assert "knowledge_retention" in conv_keys
        assert "goal_accuracy" in conv_keys

    def test_list_total_count(self):
        """测试返回的指标总数正确 (6+6+6+2=20)"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        assert len(metrics) == 20

    def test_list_entry_structure(self):
        """测试每个指标条目包含 category 和 class_name 字段"""
        metrics = DeepEvalMetricRegistry.list_available_metrics()
        for key, info in metrics.items():
            assert "category" in info, f"指标 {key} 缺少 category 字段"
            assert "class_name" in info, f"指标 {key} 缺少 class_name 字段"
            assert "description" in info, f"指标 {key} 缺少 description 字段"


@pytest.mark.usefixtures("mock_openai_api_key")
class TestCreateMetric:
    """测试创建指标实例"""

    def test_create_task_completion_default_threshold(self):
        """测试使用默认 threshold 创建 TaskCompletion 指标"""
        metric = DeepEvalMetricRegistry.create_metric("task_completion")
        assert metric.__class__.__name__ == "TaskCompletionMetric"
        assert metric.threshold == 0.5

    def test_create_with_custom_threshold(self):
        """测试使用自定义 threshold 创建指标"""
        metric = DeepEvalMetricRegistry.create_metric("answer_relevancy", threshold=0.8)
        assert metric.__class__.__name__ == "AnswerRelevancyMetric"
        assert metric.threshold == 0.8

    def test_create_with_extra_kwargs(self):
        """测试传递额外参数创建指标"""
        metric = DeepEvalMetricRegistry.create_metric(
            "faithfulness",
            threshold=0.7,
            include_reason=True,
        )
        assert metric.__class__.__name__ == "FaithfulnessMetric"
        assert metric.threshold == 0.7

    def test_create_unknown_metric_raises_error(self):
        """测试创建未知指标时抛出异常"""
        with pytest.raises(MetricNotFoundError):
            DeepEvalMetricRegistry.create_metric("unknown_metric")

    @pytest.mark.parametrize("metric_key", [
        "task_completion",
        "tool_correctness",
        "bias",
        "toxicity",
        "answer_relevancy",
        "faithfulness",
        "knowledge_retention",
        "goal_accuracy",
    ])
    def test_create_all_category_metrics(self, metric_key: str):
        """测试可以成功创建各类别的指标实例"""
        metric = DeepEvalMetricRegistry.create_metric(metric_key)
        assert metric is not None
        assert hasattr(metric, "threshold")


class TestMetricKeyNormalization:
    """测试指标 key 标准化处理"""

    def test_key_with_whitespace(self):
        """测试 key 带空格时能正确处理"""
        with pytest.raises(MetricNotFoundError):
            DeepEvalMetricRegistry.get_metric_class(" task_completion ")

    def test_case_sensitive(self):
        """测试 key 大小写敏感"""
        with pytest.raises(MetricNotFoundError):
            DeepEvalMetricRegistry.get_metric_class("Task_Completion")
