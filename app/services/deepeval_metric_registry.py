"""
DeepEval 指标注册表

提供指标的注册、查询和创建能力，支持以下类别：
- Agent 评测指标
- 安全评测指标
- RAG 评测指标
- 对话评测指标
"""
from typing import Any, Type

from deepeval.metrics import (
    # Agent 评测指标
    TaskCompletionMetric,
    ToolCorrectnessMetric,
    StepEfficiencyMetric,
    ArgumentCorrectnessMetric,
    PlanAdherenceMetric,
    PlanQualityMetric,
    # 安全评测指标
    BiasMetric,
    ToxicityMetric,
    PIILeakageMetric,
    NonAdviceMetric,
    MisuseMetric,
    RoleViolationMetric,
    # RAG 评测指标
    AnswerRelevancyMetric,
    FaithfulnessMetric,
    HallucinationMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    ContextualRelevancyMetric,
    # 对话评测指标
    KnowledgeRetentionMetric,
    GoalAccuracyMetric,
)


class MetricNotFoundError(Exception):
    """当请求的指标 key 在注册表中不存在时抛出"""

    def __init__(self, metric_key: str, available_keys: list[str]):
        self.metric_key = metric_key
        self.available_keys = available_keys
        super().__init__(
            f"未找到指标 '{metric_key}'。"
            f"可用指标: {', '.join(sorted(available_keys))}"
        )


# 指标注册表配置：key -> (metric_class, category, description)
_METRIC_REGISTRY: dict[str, tuple[Type, str, str]] = {
    # ---- Agent 评测指标 ----
    "task_completion": (
        TaskCompletionMetric,
        "agent",
        "任务完成度 - 评估 Agent 是否成功完成了指定任务",
    ),
    "tool_correctness": (
        ToolCorrectnessMetric,
        "agent",
        "工具正确性 - 评估 Agent 是否选择了正确的工具",
    ),
    "step_efficiency": (
        StepEfficiencyMetric,
        "agent",
        "步骤效率 - 评估 Agent 完成任务的步骤是否高效",
    ),
    "argument_correctness": (
        ArgumentCorrectnessMetric,
        "agent",
        "参数正确性 - 评估 Agent 调用工具时的参数是否正确",
    ),
    "plan_adherence": (
        PlanAdherenceMetric,
        "agent",
        "计划遵循度 - 评估 Agent 是否遵循了既定计划",
    ),
    "plan_quality": (
        PlanQualityMetric,
        "agent",
        "计划质量 - 评估 Agent 制定的计划质量",
    ),
    # ---- 安全评测指标 ----
    "bias": (
        BiasMetric,
        "safety",
        "偏见检测 - 检测输出中的偏见内容",
    ),
    "toxicity": (
        ToxicityMetric,
        "safety",
        "毒性检测 - 检测输出中的有害或攻击性内容",
    ),
    "pii_leakage": (
        PIILeakageMetric,
        "safety",
        "PII 泄露检测 - 检测输出中是否泄露了个人身份信息",
    ),
    "non_advice": (
        NonAdviceMetric,
        "safety",
        "非建议检测 - 检测输出是否提供了不当建议",
    ),
    "misuse": (
        MisuseMetric,
        "safety",
        "滥用检测 - 检测模型是否被滥用",
    ),
    "role_violation": (
        RoleViolationMetric,
        "safety",
        "角色违规检测 - 检测是否违反了预设角色约束",
    ),
    # ---- RAG 评测指标 ----
    "answer_relevancy": (
        AnswerRelevancyMetric,
        "rag",
        "答案相关性 - 评估生成的答案与问题的相关程度",
    ),
    "faithfulness": (
        FaithfulnessMetric,
        "rag",
        "忠实度 - 评估生成的答案是否忠实于提供的上下文",
    ),
    "hallucination": (
        HallucinationMetric,
        "rag",
        "幻觉检测 - 检测生成内容中的幻觉信息",
    ),
    "contextual_precision": (
        ContextualPrecisionMetric,
        "rag",
        "上下文精确度 - 评估检索到的上下文的精确程度",
    ),
    "contextual_recall": (
        ContextualRecallMetric,
        "rag",
        "上下文召回率 - 评估检索到的上下文的召回率",
    ),
    "contextual_relevancy": (
        ContextualRelevancyMetric,
        "rag",
        "上下文相关性 - 评估检索到的上下文与问题的相关程度",
    ),
    # ---- 对话评测指标 ----
    "knowledge_retention": (
        KnowledgeRetentionMetric,
        "conversation",
        "知识保持度 - 评估对话中知识的保持能力",
    ),
    "goal_accuracy": (
        GoalAccuracyMetric,
        "conversation",
        "目标准确度 - 评估对话是否达到了预期目标",
    ),
}


class DeepEvalMetricRegistry:
    """DeepEval 指标注册表，提供指标的查询和创建能力"""

    @classmethod
    def get_metric_class(cls, metric_key: str) -> Type:
        """
        根据指标 key 获取指标类

        Args:
            metric_key: 指标的唯一标识符，如 'task_completion'

        Returns:
            对应的 DeepEval 指标类

        Raises:
            MetricNotFoundError: 当指标 key 不存在时
        """
        if metric_key not in _METRIC_REGISTRY:
            raise MetricNotFoundError(
                metric_key=metric_key,
                available_keys=list(_METRIC_REGISTRY.keys()),
            )
        return _METRIC_REGISTRY[metric_key][0]

    @classmethod
    def list_available_metrics(cls) -> dict[str, dict[str, str]]:
        """
        列出所有可用的指标

        Returns:
            字典，key 为指标标识符，value 为包含 category, class_name, description 的字典
        """
        return {
            key: {
                "category": info[1],
                "class_name": info[0].__name__,
                "description": info[2],
            }
            for key, info in _METRIC_REGISTRY.items()
        }

    @classmethod
    def create_metric(cls, metric_key: str, threshold: float = 0.5, **kwargs: Any) -> Any:
        """
        创建指标实例

        Args:
            metric_key: 指标的唯一标识符
            threshold: 指标的阈值，默认为 0.5
            **kwargs: 传递给指标构造函数的额外参数

        Returns:
            DeepEval 指标实例

        Raises:
            MetricNotFoundError: 当指标 key 不存在时
        """
        metric_class = cls.get_metric_class(metric_key)
        return metric_class(threshold=threshold, **kwargs)

    @classmethod
    def get_category_metrics(cls, category: str) -> dict[str, Type]:
        """
        获取指定类别的所有指标

        Args:
            category: 指标类别，如 'agent', 'safety', 'rag', 'conversation'

        Returns:
            字典，key 为指标标识符，value 为指标类
        """
        return {
            key: info[0]
            for key, info in _METRIC_REGISTRY.items()
            if info[1] == category
        }
