"""LLM 裁判并发池配置与审计模型"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, JSON, String
from sqlalchemy.sql import func

from app.db.session import Base


class JudgePoolConfig(Base):
    """并发池配置（单例行）"""

    __tablename__ = "judge_pool_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    graph_limit = Column(Integer, nullable=False, default=16, comment="graph 引擎并发上限")
    claude_agent_limit = Column(Integer, nullable=False, default=8, comment="claude_agent 引擎并发上限")
    harness_limit = Column(Integer, nullable=False, default=8, comment="harness 引擎并发上限")
    plugin_limit = Column(Integer, nullable=False, default=4, comment="plugin 引擎并发上限")
    lease_ttl_seconds = Column(Integer, nullable=False, default=180, comment="租约有效期（秒）")
    heartbeat_interval_seconds = Column(Integer, nullable=False, default=30, comment="心跳续约间隔（秒）")
    wait_timeout_seconds = Column(Integer, nullable=False, default=900, comment="申请等待超时（秒）")
    scan_interval_seconds = Column(Integer, nullable=False, default=60, comment="僵尸扫描间隔（秒）")
    audit_retention_days = Column(Integer, nullable=False, default=30, comment="审计日志保留天数")
    fail_open = Column(Boolean, nullable=False, default=True, comment="Redis 不可用时是否放行")
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JudgePoolAuditLog(Base):
    """并发池事件审计"""

    __tablename__ = "judge_pool_audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event = Column(String(32), nullable=False, index=True, comment="acquire/release/wait_timeout/...")
    engine = Column(String(32), nullable=False, index=True)
    lease_id = Column(String(64), nullable=True)
    eval_task_id = Column(String(64), nullable=True)
    celery_task_id = Column(String(64), nullable=True)
    row_idx = Column(Integer, nullable=True)
    host = Column(String(128), nullable=True)
    pid = Column(Integer, nullable=True)
    wait_ms = Column(Integer, nullable=True, comment="等待耗时（申请事件）")
    hold_ms = Column(Integer, nullable=True, comment="占用时长（释放/回收事件）")
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
