"""add evaluation_results + task_execution_records columns (W3 行级结果落库)

W3-1（P0-E）：评测结果由「任务末段一次落库」改为「逐行 write-through」。
- 新增 evaluation_results 行级结果表（含计划外的 row_result 全量行结果列：
  行级表要作为聚合/断点续跑重建 result JSON 的事实源，仅靠计划 DDL 的
  投影列无法与旧行为同构重建，详见模型注释与 evaluation_result_service）；
- task_execution_records 追加 config_snapshot（W4 配置快照备用列）与
  heartbeat_at（W10 收尸 reaper 备用列），一次迁移减少次数；
  两列用 ADD COLUMN IF NOT EXISTS 幂等（W4 分支可能并行建列）。

expand-only：upgrade 只加表/加列；downgrade 仅回退本次新增对象。

Revision ID: c4e8f9a2b7d6
Revises: f8e1d4c7a2b6
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4e8f9a2b7d6'
down_revision: Union[str, None] = 'f8e1d4c7a2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- W3-1：行级结果表（新表新列一律 JSONB，符合既有 json→jsonb 方向）----
    op.create_table(
        'evaluation_results',
        sa.Column('id', sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('execution_number', sa.Integer(), nullable=False),
        sa.Column('data_index', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('input_data', postgresql.JSONB(), nullable=True),
        sa.Column('model_output', sa.Text(), nullable=True),
        sa.Column('model_raw_response', sa.Text(), nullable=True),
        sa.Column('judge_results', postgresql.JSONB(), nullable=True),
        sa.Column('aggregate', postgresql.JSONB(), nullable=True),
        sa.Column('row_result', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['task_id'], ['evaluation_tasks.id']),
        sa.UniqueConstraint('task_id', 'execution_number', 'data_index',
                           name='uq_eval_results_task_exec_data'),
    )
    op.create_index('idx_eval_results_task_exec', 'evaluation_results',
                    ['task_id', 'execution_number'])
    op.create_index('idx_eval_results_status', 'evaluation_results',
                    ['task_id', 'execution_number', 'status'])

    # ---- W4/W10 备用列：IF NOT EXISTS 幂等，W4 分支并行建列不冲突 ----
    op.execute("ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS config_snapshot JSONB")
    op.execute("ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS heartbeat_at")
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS config_snapshot")
    op.drop_index('idx_eval_results_status', table_name='evaluation_results')
    op.drop_index('idx_eval_results_task_exec', table_name='evaluation_results')
    op.drop_table('evaluation_results')
