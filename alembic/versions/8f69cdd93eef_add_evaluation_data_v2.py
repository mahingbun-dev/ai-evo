"""add evaluation_data_v2 and field mapping

Revision ID: 8f69cdd93eef
Revises: d75f472782a2
Create Date: 2026-04-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f69cdd93eef'
down_revision: Union[str, None] = 'd75f472782a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 创建 evaluation_data_v2 表
    op.create_table(
        'evaluation_data_v2',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('dataset_id', sa.String(), nullable=True),
        sa.Column('data', sa.JSON(), nullable=False, comment='自由格式的完整数据对象'),
        sa.Column('index', sa.Integer(), nullable=False, comment='排序索引'),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['dataset_id'], ['evaluation_datasets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_data_v2_dataset', 'evaluation_data_v2', ['dataset_id'])
    op.create_index('idx_data_v2_index', 'evaluation_data_v2', ['dataset_id', 'index'])

    # evaluation_tasks 新增字段映射列
    op.add_column('evaluation_tasks', sa.Column('input_field', sa.String(), server_default='user_input', nullable=True, comment='指向 data 中的用户输入字段名'))
    op.add_column('evaluation_tasks', sa.Column('reference_field', sa.String(), server_default='answer', nullable=True, comment='指向 data 中的参考答案字段名'))


def downgrade() -> None:
    op.drop_index('idx_data_v2_index', 'evaluation_data_v2')
    op.drop_index('idx_data_v2_dataset', 'evaluation_data_v2')
    op.drop_table('evaluation_data_v2')
    op.drop_column('evaluation_tasks', 'reference_field')
    op.drop_column('evaluation_tasks', 'input_field')