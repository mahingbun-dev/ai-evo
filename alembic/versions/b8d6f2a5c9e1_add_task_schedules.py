"""M5 QE5-3: 定时评测——task_schedules 表 + weekly_reports 周报归档表

- task_schedules：任务级 cron 定时（task_id 唯一，一任务一定时），
  next_run_at 为扫描 due 依据（api 进程 lifespan 周期扫描 + Redis 锁单写
  + CAS 原子推进，不引 celery beat）；
- weekly_reports：每周一 08:00（北京时间）聚合生成的周报归档
  （week_start 唯一幂等；markdown 正文 + 指标快照）。

expand-only：仅新增两张表，不动存量数据。单 head（f6b2d9e4c8a3 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M5 QE5-3。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d6f2a5c9e1'
down_revision = 'f6b2d9e4c8a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "task_schedules" not in inspector.get_table_names():
        op.create_table(
            'task_schedules',
            sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：sch_{uuid12}'),
            sa.Column('task_id', sa.String(),
                      sa.ForeignKey('evaluation_tasks.id', ondelete='CASCADE'),
                      nullable=False, comment='评测任务ID（唯一，一任务一定时）'),
            sa.Column('cron', sa.String(100), nullable=False, comment='cron 表达式（5 段，北京时间）'),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true',
                      comment='是否启用（停用后不扫描，配置保留）'),
            sa.Column('window_start', sa.String(5), nullable=True, comment='允许触发窗口开始 HH:MM（空=不限）'),
            sa.Column('window_end', sa.String(5), nullable=True, comment='允许触发窗口结束 HH:MM（空=不限）'),
            sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True, comment='最近一次触发尝试时间'),
            sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True,
                      comment='下次应触发时间（扫描 due 依据，CAS 原子推进）'),
            sa.Column('last_status', sa.String(30), nullable=True,
                      comment='最近一次结果：dispatched/skipped_running/skipped_window/submit_rejected/task_unavailable'),
            sa.Column('last_message', sa.String(500), nullable=True, comment='最近一次结果说明'),
            sa.Column('history', sa.JSON(), nullable=True, comment='触发历史（最近 20 条，新在前）'),
            sa.Column('created_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('task_id', name='uq_task_schedules_task'),
            comment='任务定时调度表（M5 QE5-3）',
        )
        op.create_index('ix_task_schedules_id', 'task_schedules', ['id'])
        op.create_index('ix_task_schedules_task_id', 'task_schedules', ['task_id'])
        op.create_index('ix_task_schedules_next_run_at', 'task_schedules', ['next_run_at'])

    if "weekly_reports" not in inspector.get_table_names():
        op.create_table(
            'weekly_reports',
            sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：wr_{uuid12}'),
            sa.Column('week_start', sa.Date(), nullable=False, comment='报告覆盖周起始日期（周一）'),
            sa.Column('week_end', sa.Date(), nullable=False, comment='报告覆盖周结束日期（周日）'),
            sa.Column('content', sa.Text(), nullable=False, comment='周报正文（markdown）'),
            sa.Column('payload', sa.JSON(), nullable=True, comment='聚合指标快照'),
            sa.Column('pushed_webhooks', sa.Integer(), nullable=False, server_default='0',
                      comment='推送命中的 webhook 端点数（留痕行数）'),
            sa.Column('email_status', sa.String(20), nullable=True,
                      comment='邮件推送结果：sent/skipped/failed（SMTP 未配置=skipped）'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('week_start', name='uq_weekly_reports_week'),
            comment='周报归档表（M5 QE5-3）',
        )
        op.create_index('ix_weekly_reports_id', 'weekly_reports', ['id'])
        op.create_index('ix_weekly_reports_week_start', 'weekly_reports', ['week_start'])


def downgrade() -> None:
    op.drop_index('ix_weekly_reports_week_start', table_name='weekly_reports')
    op.drop_index('ix_weekly_reports_id', table_name='weekly_reports')
    op.drop_table('weekly_reports')
    op.drop_index('ix_task_schedules_next_run_at', table_name='task_schedules')
    op.drop_index('ix_task_schedules_task_id', table_name='task_schedules')
    op.drop_index('ix_task_schedules_id', table_name='task_schedules')
    op.drop_table('task_schedules')
