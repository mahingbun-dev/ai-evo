"""
A/B 测试会话数据模型
"""
from sqlalchemy import Column, String, DateTime, JSON, Float
from sqlalchemy.sql import func
from app.db.session import Base


class ABTestSession(Base):
    """A/B 测试会话记录表"""
    __tablename__ = "ab_test_sessions"

    id = Column(String, primary_key=True, index=True, comment="主键，格式：abtest_{uuid12}")
    name = Column(String, nullable=True, comment="可选备注名")

    # 模型信息（冗余存储方便列表展示）
    model_a_id = Column(String, nullable=False, comment="模型A的配置ID")
    model_b_id = Column(String, nullable=False, comment="模型B的配置ID")
    model_a_name = Column(String, nullable=True, comment="模型A名称（冗余）")
    model_b_name = Column(String, nullable=True, comment="模型B名称（冗余）")

    # 输入变量（{var: {value, type}} 格式，与WS协议一致）
    variables = Column(JSON, nullable=True, comment="共享变量配置")

    # 执行结果（{success, content, request_details[]}）
    result_a = Column(JSON, nullable=True, comment="模型A的完整执行结果")
    result_b = Column(JSON, nullable=True, comment="模型B的完整执行结果")

    # 状态与耗时
    status = Column(String, default="completed", comment="状态：completed/failed")
    duration_a = Column(Float, nullable=True, comment="模型A耗时（秒）")
    duration_b = Column(Float, nullable=True, comment="模型B耗时（秒）")

    # W19 归属：创建人 user_code。历史行为 NULL——读对所有登录用户可见，
    # 删除仅创建人或超管（与 access_control 团队口径互补的轻量归属）。
    created_by = Column(String, nullable=True, comment="创建人 user_code（历史行为 NULL）")

    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
