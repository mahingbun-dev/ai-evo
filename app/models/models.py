from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Text, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class BaseModel(Base):
    """模型基类，包含逻辑删除标识 is_delete"""

    __abstract__ = True

    is_delete = Column(Boolean, default=False, nullable=False, server_default="false")


class ExcelFile(BaseModel):
    """Excel 文件元数据表"""
    __tablename__ = "excel_files"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    upload_time = Column(DateTime(timezone=True), server_default=func.now())
    description = Column(String, nullable=True)

class ExcelRow(BaseModel):
    """Excel 行数据表，使用 JSONB 存储任意结构"""
    __tablename__ = "excel_rows"

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(Integer, ForeignKey("excel_files.id", ondelete="CASCADE"))
    row_index = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)  # 存储整行数据的字典格式

