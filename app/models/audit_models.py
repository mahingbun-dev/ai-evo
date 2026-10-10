"""
操作审计日志数据模型
记录所有写操作的审计信息
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON
from sqlalchemy.sql import func
from app.db.session import Base


class OperationLog(Base):
    """操作审计日志表"""
    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="日志ID")
    user_code = Column(String, nullable=False, index=True, comment="操作者用户代码")
    user_name = Column(String, nullable=True, comment="操作者姓名")
    team_code = Column(String, nullable=True, index=True, comment="操作者所属团队")
    action = Column(String, nullable=False, comment="操作类型：CREATE/UPDATE/DELETE")
    resource_type = Column(String, nullable=False, index=True, comment="资源类型：task/dataset/model/template/media")
    resource_id = Column(String, nullable=False, comment="资源ID")
    resource_name = Column(String, nullable=True, comment="资源名称")
    detail = Column(JSON, nullable=True, comment="变更详情")
    ip_address = Column(String, nullable=True, comment="客户端IP地址")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="操作时间")
