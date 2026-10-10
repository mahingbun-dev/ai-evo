"""
评测数据集数据模型
"""
import sqlalchemy as sa
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, ForeignKey, Index
from sqlalchemy.sql import func
from app.db.session import Base


class EvaluationDataset(Base):
    """评测数据集表"""
    __tablename__ = "evaluation_datasets"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="数据集名称")
    # 部分唯一索引：只对 is_active=True 的记录生效，允许软删除后重建同名数据集
    __table_args__ = (
        Index('idx_evaluation_datasets_name_active', 'name', postgresql_where=sa.text('is_active = true')),
    )
    description = Column(String, nullable=True, comment="数据集描述")
    question_count = Column(Integer, default=0, comment="题目数量")

    created_by = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")


class EvaluationData(Base):
    """评测数据表"""
    __tablename__ = "evaluation_data"

    id = Column(String, primary_key=True, index=True)
    dataset_id = Column(String, ForeignKey("evaluation_datasets.id", ondelete="CASCADE"))
    index = Column(Integer, nullable=False, comment="题目序号")

    # 用户输入和答案
    question = Column(String, nullable=True, comment="用户输入")
    ref_answer = Column(String, nullable=True, comment="参考答案")

    # 元数据（包含answer_is_media等）
    meta_data = Column(JSON, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EvaluationDataV2(Base):
    """评测数据表 V2 - 自由格式"""
    __tablename__ = "evaluation_data_v2"

    id = Column(String, primary_key=True, index=True)
    dataset_id = Column(String, ForeignKey("evaluation_datasets.id", ondelete="CASCADE"))
    data = Column(JSON, nullable=False, comment="自由格式的完整数据对象")
    index = Column(Integer, nullable=False, comment="排序索引")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index('idx_data_v2_dataset', 'dataset_id'),
        Index('idx_data_v2_index', 'dataset_id', 'index'),
    )


class DatasetFieldMetadata(Base):
    """数据集字段元数据表 - 存储数据集的字段结构定义"""
    __tablename__ = "dataset_field_metadata"

    id = Column(String, primary_key=True, index=True)
    dataset_id = Column(String, ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    field_name = Column(String, nullable=False, comment="字段名")
    field_label = Column(String, nullable=True, comment="字段显示名")
    field_type = Column(String, default="string", comment="字段类型：string/number/boolean")
    is_media = Column(Boolean, default=False, comment="是否为媒体字段")
    is_media_field = Column(String, nullable=True, comment="关联的is_media字段名")
    is_required = Column(Boolean, default=True, comment="导入时是否必填")
    position = Column(Integer, default=0, comment="字段顺序")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DatasetSnapshot(Base):
    """数据集快照表 - 存储任务执行时数据集的快照"""
    __tablename__ = "dataset_snapshot"

    id = Column(String, primary_key=True, index=True)
    dataset_id = Column(String, ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    data = Column(JSON, nullable=False, comment="快照数据完整拷贝")
    fields = Column(JSON, nullable=False, comment="快照时的字段结构")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ImportTempFile(Base):
    """导入临时文件表 - 用于持久化存储预览阶段上传的临时文件"""
    __tablename__ = "import_temp_files"

    id = Column(String, primary_key=True, index=True)
    preview_id = Column(String, nullable=False, index=True, comment="预览ID")
    data_index = Column(Integer, nullable=False, comment="数据索引")
    field = Column(String, nullable=False, comment="字段名：user_input 或 answer")

    # 文件信息
    file_name = Column(String, nullable=False, comment="原始文件名")
    file_path = Column(String, nullable=False, comment="文件存储路径")
    file_type = Column(String, nullable=True, comment="文件类型")
    file_size = Column(Integer, nullable=True, comment="文件大小")

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
