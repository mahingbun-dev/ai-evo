"""
用户数据模型
"""
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class User(Base):
    """用户表"""
    __tablename__ = "users"

    user_code = Column(String, primary_key=True, index=True, comment="用户代码")
    username = Column(String, unique=True, index=True, nullable=True, comment="登录用户名（本地账号）")
    password_hash = Column(String, nullable=True, comment="密码哈希（PBKDF2，本地账号）")
    user_name = Column(String, nullable=True, comment="姓名")
    email = Column(String, nullable=True, comment="邮箱")
    employee_id = Column(String, nullable=True, comment="工号")
    org_code = Column(String, nullable=True, comment="部门编码")
    org_name = Column(String, nullable=True, comment="部门名称")
    position_name = Column(String, nullable=True, comment="职位名称")
    # 团队角色（M4 QE4-3）：owner/engineer/viewer；NULL 按 engineer 兜底
    # （迁移回填后仅历史行可能 NULL，语义缺省=engineer，见 access_control.user_role）
    role = Column(String(20), nullable=True, comment="团队角色：owner/engineer/viewer（空按 engineer）")

    user_type = Column(Integer, nullable=True, comment="用户类型")
    super_admin_flag = Column(Integer, nullable=True, comment="超级管理员标志")
    application_admin = Column(Integer, nullable=True, comment="应用管理员标志")

    is_active = Column(Boolean, default=True, comment="是否启用")

    last_login_at = Column(DateTime(timezone=True), nullable=True, comment="最近登录时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="首次登录时间")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="信息更新时间")
