"""
模型模块
统一导出所有数据模型
"""
from app.models.models import BaseModel, ExcelFile, ExcelRow
from app.models.model_config_models import ModelConfig, PresetModel, JudgeModelConfig
from app.models.claude_code_models import ClaudeCodeConfig
from app.models.deepseek_harness_models import DeepSeekHarnessConfig
from app.models.pre_judge_models import PreJudgeScript
from app.models.media_models import MediaResource
from app.models.dataset_models import EvaluationDataset, EvaluationData
from app.models.hub_file_models import HubFileRecord, HubImportJob
from app.models.task_models import EvaluationTask, TaskExecutionLog, TaskExecutionRecord, EvaluationResult
from app.models.template_models import EvaluationTemplate, MultiJudgeConfig, EvaluationMetric
from app.models.user_models import User
from app.models.team_models import Team
from app.models.audit_models import OperationLog
from app.models.stats_models import TaskExecutionStats, WeeklyCoverageConfig, WeeklyPlanTask
from app.models.plugin_marketplace_models import PluginMarketplace
from app.models.api_key_models import ApiKey
from app.models.webhook_models import WebhookEndpoint, WebhookDelivery
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping, LdapConfig

__all__ = [
    # 原有模型
    "BaseModel",
    "ExcelFile",
    "ExcelRow",
    # 模型配置
    "ModelConfig",
    "PresetModel",
    "JudgeModelConfig",
    # Claude Code 全局配置
    "ClaudeCodeConfig",
    # 评测前处理脚本
    "PreJudgeScript",
    # DeepSeek Harness 全局配置
    "DeepSeekHarnessConfig",
    # 媒体资源
    "MediaResource",
    # 数据集
    "EvaluationDataset",
    "EvaluationData",
    # anc-hub 执行包登记（数据集迭代新增）
    "HubFileRecord",
    # anc-hub 执行包批量导入任务（数据集迭代新增）
    "HubImportJob",
    # 任务
    "EvaluationTask",
    "TaskExecutionLog",
    # 任务执行历史记录
    "TaskExecutionRecord",
    # 评测行级结果（W3 行级落库）
    "EvaluationResult",
    # 模板
    "EvaluationTemplate",
    "MultiJudgeConfig",
    "EvaluationMetric",
    # 用户
    "User",
    # 团队（V2 新增）
    "Team",
    # 审计日志（V2 新增）
    "OperationLog",
    # 任务执行统计（V2 新增）
    "TaskExecutionStats",
    "WeeklyCoverageConfig",
    "WeeklyPlanTask",
    # 插件市场（插件评测评测对象来源）
    "PluginMarketplace",
    # API Key（M1 QE1-1，门禁闭环 CI/CLI 凭证）
    "ApiKey",
    # Webhook（M1 QE1-3，门禁闭环通知面）
    "WebhookEndpoint",
    "WebhookDelivery",
    # OIDC SSO（M4 QE4-1，企业身份）
    "SsoOidcConfig",
    "SsoTeamMapping",
]
