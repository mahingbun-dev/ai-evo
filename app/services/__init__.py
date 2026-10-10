"""
评测服务模块

提供直接评测能力，无需通过 pytest 启动。
"""

# Token 管理器
from .token_manager import (
    TokenManager,
    TokenInfo,
    get_token_manager,
    configure_token_manager,
    get_sso_token,
)

# DeepEval 适配器
from . import deepeval_adapter as DeepEvalMetricAdapter
from .deepeval_metric_registry import DeepEvalMetricRegistry, MetricNotFoundError
from .deepeval_result_transformer import DeepEvalResultTransformer

# Agent 评测器
from .agent_evaluator import AgentEvaluator

__all__ = [
    # Token Manager
    "TokenManager",
    "TokenInfo",
    "get_token_manager",
    "configure_token_manager",
    "get_sso_token",
    # DeepEval
    "DeepEvalMetricAdapter",
    "DeepEvalMetricRegistry",
    "DeepEvalResultTransformer",
    "MetricNotFoundError",
    # Agent Evaluator
    "AgentEvaluator",
]
