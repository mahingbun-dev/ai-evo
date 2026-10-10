"""
媒体资源数据模型
用于存储图片、PDF等文件
"""
from sqlalchemy import Column, Integer, String, DateTime, LargeBinary, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class MediaResource(Base):
    """媒体资源表（存储图片、PDF等文件）"""
    __tablename__ = "media_resources"

    id = Column(String, primary_key=True, index=True)
    file_name = Column(String, nullable=False, comment="文件原始名称")
    file_type = Column(String, nullable=False, comment="文件类型：image/pdf/docx/xlsx等")
    mime_type = Column(String, nullable=True, comment="MIME类型")
    file_size = Column(Integer, nullable=True, comment="文件大小（字节）")

    # 存储方式二选一：
    # 方式1：数据库存储（二进制）
    file_data = Column(LargeBinary, nullable=True, comment="文件二进制数据（bytea）")
    # 方式2：文件路径存储
    file_path = Column(String, nullable=True, comment="文件存储路径")

    description = Column(String, nullable=True, comment="文件描述")
    uploaded_by = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")
