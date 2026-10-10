"""任务执行统计模型"""
from sqlalchemy import Column, String, DateTime, BigInteger, Integer, Date, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from app.db.session import Base


class TaskExecutionStats(Base):
    """任务执行统计表"""
    __tablename__ = "task_execution_stats"

    id = Column(String, primary_key=True, index=True)
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    evaluation_type = Column(String, nullable=False, comment="任务类型")
    model_config_id = Column(String, nullable=True, comment="模型配置ID")
    dataset_id = Column(String, nullable=True, comment="数据集ID")
    status = Column(String, nullable=False, default="pending", comment="状态：pending/running/success/failed")
    started_at = Column(DateTime(timezone=True), nullable=True, comment="开始时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="结束时间")
    duration_ms = Column(BigInteger, nullable=True, comment="耗时毫秒")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="记录创建时间")


class WeeklyCoverageConfig(Base):
    """周覆盖度配置表（全局单行配置）"""
    __tablename__ = "weekly_coverage_config"

    id = Column(String, primary_key=True, index=True)
    week_start_day = Column(String(20), nullable=False, default="monday", comment="周起始日")
    week_end_day = Column(String(20), nullable=False, default="sunday", comment="周结束日")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")


class WeeklyPlanTask(Base):
    """每周计划任务数"""
    __tablename__ = "weekly_plan_tasks"

    id = Column(String, primary_key=True, index=True)
    week_start_date = Column(Date, nullable=False, comment="周起始日期")
    week_end_date = Column(Date, nullable=False, comment="周结束日期")
    planned_count = Column(Integer, nullable=False, comment="本周计划任务数")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        UniqueConstraint("week_start_date", "week_end_date", name="uq_weekly_plan"),
    )
