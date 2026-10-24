"""add judge pool configs/audit and task concurrency

Revision ID: c7e3f1a9b2d4
Revises: a7c2f9e4b1d3
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7e3f1a9b2d4"
down_revision: Union[str, None] = "a7c2f9e4b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "judge_pool_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("graph_limit", sa.Integer(), nullable=False, server_default="16"),
        sa.Column("claude_agent_limit", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("harness_limit", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("plugin_limit", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("lease_ttl_seconds", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("heartbeat_interval_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("wait_timeout_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("audit_retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("fail_open", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "judge_pool_audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("lease_id", sa.String(64), nullable=True),
        sa.Column("eval_task_id", sa.String(64), nullable=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("row_idx", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(128), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("wait_ms", sa.Integer(), nullable=True),
        sa.Column("hold_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_judge_pool_audit_event", "judge_pool_audit_logs", ["event"])
    op.create_index("ix_judge_pool_audit_engine", "judge_pool_audit_logs", ["engine"])
    op.create_index("ix_judge_pool_audit_created", "judge_pool_audit_logs", ["created_at"])
    # 幂等写法（仓库惯例，见 a1b2c3d4e5f6 / a7c2f9e4b1d3）：迁移探针测试以
    # create_all 建基线后再重放迁移链，此处列已由当前 ORM 元数据建出，
    # 普通 ADD COLUMN 会 DuplicateColumn
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS "
        "concurrency INTEGER DEFAULT '4'"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.concurrency IS '行级并发数'"
    )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "concurrency")
    op.drop_index("ix_judge_pool_audit_created", table_name="judge_pool_audit_logs")
    op.drop_index("ix_judge_pool_audit_engine", table_name="judge_pool_audit_logs")
    op.drop_index("ix_judge_pool_audit_event", table_name="judge_pool_audit_logs")
    op.drop_table("judge_pool_audit_logs")
    op.drop_table("judge_pool_configs")
