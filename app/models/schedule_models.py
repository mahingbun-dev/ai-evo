"""定时评测与周报数据模型（M5 QE5-3）

- task_schedules：任务级 cron 定时（每任务至多一条）。调度由 api 进程
  lifespan 周期扫描承担（不引 celery beat，见 app/main.py 与
  app/services/task_schedule_service.py），next_run_at 是扫描 due 的唯一
  依据——被 CAS 原子推进后即视为该次触发已被认领，多 api 单写靠 Redis 锁
  + CAS 双保险；
- weekly_reports：每周一 08:00（北京时间）聚合的周报归档（markdown 正文
  + 指标快照），week_start 唯一保证生成幂等；推送走既有 webhook 事件面
  （weekly.report.ready），SMTP 为可选配置（未配置即跳过，不告警）。
"""
from sqlalchemy import (
    Column, String, DateTime, Date, Integer, Boolean, Text, ForeignKey,
    UniqueConstraint, JSON,
)
from sqlalchemy.sql import func

from app.db.session import Base


class TaskSchedule(Base):
    """任务定时调度（每任务一条；M5 QE5-3）"""
    __tablename__ = "task_schedules"

    id = Column(String, primary_key=True, index=True)
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True, comment="评测任务ID（唯一，一任务一定时）")
    # 5 段 cron：分 时 日 月 周（北京时间；支持 * / , - 与 */n 步进）
    cron = Column(String(100), nullable=False, comment="cron 表达式（5 段，北京时间）")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true",
                     comment="是否启用（停用后不扫描，配置保留）")
    # 时段限制（可空）：HH:MM，两者都配才生效——cron 触发时刻须落在窗口内
    window_start = Column(String(5), nullable=True, comment="允许触发窗口开始 HH:MM（空=不限）")
    window_end = Column(String(5), nullable=True, comment="允许触发窗口结束 HH:MM（空=不限）")
    last_run_at = Column(DateTime(timezone=True), nullable=True, comment="最近一次触发尝试时间")
    # 下次应触发时间：due 判定依据；扫描进程 CAS 推进（rowcount=1 才认领）
    next_run_at = Column(DateTime(timezone=True), nullable=True, index=True,
                         comment="下次应触发时间（扫描 due 依据，CAS 原子推进）")
    last_status = Column(String(30), nullable=True,
                         comment="最近一次结果：dispatched/skipped_running/skipped_window/submit_rejected/task_unavailable")
    last_message = Column(String(500), nullable=True, comment="最近一次结果说明")
    # 运行历史（最近 N 条，新在前）：[{at, status, message, execution_number}]，
    # 轻量内嵌而非独立表——历史仅用于排障展示，无关联查询诉求
    history = Column(JSON, nullable=True, comment="触发历史（最近 20 条，新在前）")
    created_by = Column(String, nullable=True, comment="创建人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        UniqueConstraint("task_id", name="uq_task_schedules_task"),
    )


class WeeklyReport(Base):
    """周报归档（每周一 08:00 聚合生成，week_start 幂等；M5 QE5-3）"""
    __tablename__ = "weekly_reports"

    id = Column(String, primary_key=True, index=True)
    # 报告覆盖的自然周（周一日期）；唯一约束保证「一周一报、错过补生成不重复」
    week_start = Column(Date, nullable=False, index=True, comment="报告覆盖周起始日期（周一）")
    week_end = Column(Date, nullable=False, comment="报告覆盖周结束日期（周日）")
    # markdown 正文（渲染风格对齐 app/services/result_report.py 的分节口径）
    content = Column(Text, nullable=False, comment="周报正文（markdown）")
    # 指标快照（覆盖度/提效/门禁通过率等原始数值，供前端二次消费与快照测试）
    payload = Column(JSON, nullable=True, comment="聚合指标快照")
    pushed_webhooks = Column(Integer, nullable=False, default=0, server_default="0",
                             comment="推送命中的 webhook 端点数（留痕行数）")
    email_status = Column(String(20), nullable=True,
                          comment="邮件推送结果：sent/skipped/failed（SMTP 未配置=skipped）")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="生成时间")

    __table_args__ = (
        UniqueConstraint("week_start", name="uq_weekly_reports_week"),
    )
