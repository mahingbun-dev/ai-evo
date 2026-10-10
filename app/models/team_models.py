"""
团队数据模型
基于部门/组织信息自动同步，用于资源隔离
"""
from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class Team(Base):
    """团队表"""
    __tablename__ = "teams"

    team_code = Column(String, primary_key=True, index=True, comment="团队代码（组织编码）")
    team_name = Column(String, nullable=True, comment="团队名称（组织名称）")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="更新时间")
