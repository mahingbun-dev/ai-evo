"""性能 P0-3：evaluation_tasks / task_execution_stats 高频查询补索引

Revision ID: d41f8b2c6a90
Revises: 79626e69a75a
Create Date: 2026-10-25

消除三处全表扫描（证据见 plans/2026-10-25-性能与并发提升分析.md B2-3/B2-5）：
- team 并发配额对账 count(team_code, status)——每次任务提交的同步前置路径
  （app/services/task_scheduler.py）
- 任务列表过滤排序（status / evaluation_type / created_at desc）
- 收尸 reaper 与统计聚合的 status / created_at 过滤

注意：evaluation_tasks 带大 JSONB 列（result/gate_config 等），生产大库
建索引建议低峰期手工 CREATE INDEX CONCURRENTLY；本迁移走普通 create_index
（开发库与测试库数据量小，锁表代价可忽略），CONCURRENTLY 无法在 alembic
事务块内执行。
"""
from typing import Union

from alembic import op

revision: str = "d41f8b2c6a90"
down_revision: Union[str, None] = "79626e69a75a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_evaluation_tasks_team_status",
        "evaluation_tasks",
        ["team_code", "status"],
    )
    op.create_index(
        "ix_evaluation_tasks_status",
        "evaluation_tasks",
        ["status"],
    )
    op.create_index(
        "ix_evaluation_tasks_created_at",
        "evaluation_tasks",
        ["created_at"],
    )
    op.create_index(
        "ix_task_execution_stats_created_at",
        "task_execution_stats",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_execution_stats_created_at", table_name="task_execution_stats")
    op.drop_index("ix_evaluation_tasks_created_at", table_name="evaluation_tasks")
    op.drop_index("ix_evaluation_tasks_status", table_name="evaluation_tasks")
    op.drop_index("ix_evaluation_tasks_team_status", table_name="evaluation_tasks")
