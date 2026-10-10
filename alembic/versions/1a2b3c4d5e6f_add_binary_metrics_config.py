"""add_binary_metrics_config

Revision ID: 1a2b3c4d5e6f
Revises: 8f69cdd93eef
Create Date: 2026-04-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = '8f69cdd93eef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('evaluation_tasks', sa.Column('binary_metrics_config', sa.JSON(), nullable=True, comment='二分分类评测配置'))


def downgrade() -> None:
    op.drop_column('evaluation_tasks', 'binary_metrics_config')
