"""
评测模板数据模型
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, Text
from sqlalchemy.sql import func
from app.db.session import Base


class EvaluationTemplate(Base):
    """评测模板表"""
    __tablename__ = "evaluation_templates"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="模板名称")
    scene = Column(String, nullable=True, comment="评测场景")
    scene_description = Column(Text, nullable=True, comment="场景描述")
    judge_prompt = Column(Text, nullable=True, comment="评测指令")

    # 评测维度配置（JSON格式存储）
    evaluation_metrics = Column(JSON, nullable=True, comment="评测维度配置")

    # 评分标准
    score_details = Column(Text, nullable=True, comment="评分标准")
    max_score = Column(Integer, default=10, comment="最大分值")

    # 评分步骤
    steps = Column(Text, nullable=True, comment="评分步骤")

    # 完整指令模板
    instruction_template = Column(Text, nullable=True, comment="完整指令模板")

    # 裁判变量配置
    judge_variable_config = Column(JSON, nullable=True, comment="裁判变量配置")

    is_preset = Column(Boolean, default=False, comment="是否预制模板")
    is_active = Column(Boolean, default=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")


class MultiJudgeConfig(Base):
    """多裁判配置表"""
    __tablename__ = "multi_judge_configs"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="配置名称")

    # 裁判配置
    judge_count = Column(Integer, default=1, comment="裁判数量")
    judge_model = Column(String, nullable=True, comment="裁判模型")
    use_same_config = Column(Boolean, default=True, comment="是否使用同一裁判配置")

    # 各裁判实例配置
    judge_instances = Column(JSON, nullable=True, comment="裁判实例配置列表")

    # 聚合方式：average/weighted/consensus
    aggregation_method = Column(String, default="average", comment="聚合方式")

    # 是否启用评分理由分析
    enable_reasoning_analysis = Column(Boolean, default=True)

    # 描述
    description = Column(String, nullable=True)

    is_active = Column(Boolean, default=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")


class EvaluationMetric(Base):
    """评测维度表（可复用）"""
    __tablename__ = "evaluation_metrics"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="维度名称")
    description = Column(String, nullable=True, comment="维度描述")

    # 权重：high/medium/low 或具体数值
    weight = Column(String, nullable=True, comment="权重")
    max_score = Column(Integer, default=10, comment="最大分值")

    # 评分标准（JSON格式）
    score_criteria = Column(JSON, nullable=True, comment="评分标准")

    is_preset = Column(Boolean, default=False, comment="是否预置")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
