"""
DeepSeek Harness 配置数据模型
全局单例配置：DeepSeek Agent 形态裁判 / agent 类脚本执行器共用的
deepseek-harness-sdk 连接参数（设计：docs/design/pre-judge-scripts.md 第九节）
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class DeepSeekHarnessConfig(Base):
    """DeepSeek Harness 全局配置表（单例语义：仅一条 is_active=True 记录）"""
    __tablename__ = "deepseek_harness_configs"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, default="默认配置", comment="配置名称")

    # DeepSeek 连接参数（base_url/api_key 可空：SDK 回落环境变量 DEEPSEEK_*）
    base_url = Column(String, nullable=True, comment="DeepSeek API 地址（OpenAI 兼容；空则用 SDK 默认/环境变量）")
    api_key = Column(String, nullable=True, comment="DEEPSEEK_API_KEY（空则用 SDK 默认/环境变量）")
    protocol = Column(String, nullable=False, default="deepseek", server_default="deepseek",
                      comment="网关 API 协议：deepseek（官方适配器）| openai（OpenAI 兼容）| anthropic（Anthropic Messages）")
    provider = Column(String, nullable=False, default="deepseek-official", comment="DSH provider 标识")
    model = Column(String, nullable=False, default="deepseek-v4-flash", comment="模型名")
    profile = Column(String, nullable=False, default="sdk-minimal", comment="DSH profile（默认 sdk-minimal，danger-full-access，须隔离运行）")

    # 执行参数
    timeout_seconds = Column(Integer, default=300, comment="单次裁判调用超时（秒）")
    max_tokens = Column(Integer, default=49152, comment="单次生成最大 token")
    extra_env = Column(JSON, nullable=True, comment="附加环境变量（JSON 对象，注入 harness 子进程）")
    extra_patches = Column(JSON, nullable=True, comment="附加 patch（JSON 数组，随 SDK 启动 patches 传入）")

    is_active = Column(Boolean, default=True, comment="是否激活（单例）")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
