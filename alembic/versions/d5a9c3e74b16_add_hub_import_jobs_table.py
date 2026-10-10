"""add hub_import_jobs table

anc-hub 执行包批量导入任务表（数据集迭代：anc-hub 执行包导入 + 脚本文件契约）：
- hub_import_jobs：dataset_id 索引、file_ids/fail_detail/succeeded_file_ids JSON、
  计数列与状态列（retry 只重跑失败项的幂等键）

Revision ID: d5a9c3e74b16
Revises: c2f7a9d41e08
Create Date: 2026-10-08 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5a9c3e74b16'
down_revision: Union[str, None] = 'c2f7a9d41e08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'hub_import_jobs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('dataset_id', sa.String(), nullable=False, comment='目标数据集 ID'),
        sa.Column('file_ids', sa.JSON(), nullable=False, comment='待导入的 anc-hub file_id 列表'),
        sa.Column('status', sa.String(), nullable=False, server_default='pending',
                  comment='任务状态：pending|running|completed|failed'),
        sa.Column('total_count', sa.Integer(), nullable=False, server_default='0', comment='总包数'),
        sa.Column('success_count', sa.Integer(), nullable=False, server_default='0', comment='成功包数'),
        sa.Column('fail_count', sa.Integer(), nullable=False, server_default='0', comment='失败包数'),
        sa.Column('fail_detail', sa.JSON(), nullable=True, comment='失败明细（{file_id: 错误信息}）'),
        sa.Column('succeeded_file_ids', sa.JSON(), nullable=True, comment='已成功导入的 file_id 列表'),
        sa.Column('created_by', sa.String(), nullable=True, comment='创建人'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_hub_import_jobs_id', 'hub_import_jobs', ['id'])
    op.create_index('ix_hub_import_jobs_dataset_id', 'hub_import_jobs', ['dataset_id'])


def downgrade() -> None:
    op.drop_index('ix_hub_import_jobs_dataset_id', table_name='hub_import_jobs')
    op.drop_index('ix_hub_import_jobs_id', table_name='hub_import_jobs')
    op.drop_table('hub_import_jobs')
