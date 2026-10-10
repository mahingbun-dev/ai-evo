"""
模型配置数据模型
用于管理待评测模型和裁判员模型的配置
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, Text
from sqlalchemy.sql import func
from app.db.session import Base


class ModelConfig(Base):
    """模型配置表"""
    __tablename__ = "model_configs"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="模型名称（如：GPT-4、Claude-3、内部模型A）")
    provider = Column(String, nullable=False, default="custom_http", comment="模型提供商：openai/anthropic/custom_http")
    model_type = Column(String, nullable=False, comment="具体模型名称（如 gpt-4、claude-3-opus-20240229）")
    model_role = Column(String, default="evaluation", comment="模型角色：evaluation(待评测模型)/judge(裁判模型)")

    # 基础配置
    endpoint = Column(String, nullable=True, comment="API端点URL")
    api_key = Column(String, nullable=True, comment="API Key（加密存储）")

    # 鉴权配置
    auth_type = Column(String, default="none", comment="鉴权类型：none/api_key/bearer/basic/oauth/custom_header/query")
    auth_config = Column(JSON, nullable=True, comment="鉴权配置")

    # 请求配置
    request_config = Column(JSON, nullable=True, comment="请求配置")

    # 响应解析配置
    response_parse_config = Column(JSON, nullable=True, comment="响应解析配置")

    # 多模态支持
    supports_multimodal = Column(Boolean, default=False, comment="是否支持多模态输入")
    multimodal_config = Column(JSON, nullable=True, comment="多模态配置")

    # 其他配置
    description = Column(String, nullable=True)
    role_description = Column(String, nullable=True, comment="模型角色说明：evaluation-待评测模型，judge-裁判模型")
    is_active = Column(Boolean, default=True, comment="是否启用")
    is_preset = Column(Boolean, default=False, comment="是否预置模型")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")


class PresetModel(Base):
    """预置模型配置（系统预设）"""
    __tablename__ = "preset_models"

    id = Column(String, primary_key=True, index=True)
    provider = Column(String, nullable=False, comment="openai/anthropic/custom")
    model_name = Column(String, nullable=False, comment="模型标识")
    display_name = Column(String, nullable=False, comment="显示名称")
    default_config = Column(JSON, nullable=True, comment="默认配置")
    is_multimodal = Column(Boolean, default=False, comment="是否支持多模态")
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class JudgeModelConfig(Base):
    """裁判员模型配置"""
    __tablename__ = "judge_model_configs"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="裁判模型名称")
    model_type = Column(String, nullable=False, comment="模型类型")
    endpoint = Column(String, nullable=True)
    api_key = Column(String, nullable=True)

    # 裁判模型特定配置
    temperature = Column(Integer, default=0, comment="温度参数")
    max_tokens = Column(Integer, default=4096, comment="最大token数")
    system_prompt = Column(Text, nullable=True, comment="系统提示词")

    # 评分配置
    default_max_score = Column(Integer, default=10, comment="默认最大分值")

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
