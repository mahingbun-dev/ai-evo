"""change_name_unique_constraint_to_partial_index

Revision ID: ca64053e7c46
Revises: 5a64da264cdd
Create Date: 2026-04-08 17:29:17.866408

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ca64053e7c46'
down_revision: Union[str, None] = '5a64da264cdd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 删除原有的唯一约束（如果存在）
    op.execute("ALTER TABLE evaluation_datasets DROP CONSTRAINT IF EXISTS evaluation_datasets_name_key")
    # 创建部分唯一索引：只对 is_active=True 的记录生效
    op.execute("""
        CREATE UNIQUE INDEX idx_evaluation_datasets_name_active
        ON evaluation_datasets (name)
        WHERE is_active = true
    """)


def downgrade() -> None:
    # 删除部分唯一索引
    op.execute("DROP INDEX IF EXISTS idx_evaluation_datasets_name_active")
    # 恢复原有的唯一约束
    op.execute("ALTER TABLE evaluation_datasets ADD CONSTRAINT evaluation_datasets_name_key UNIQUE (name)")
