"""add supplement_index to hub_files

执行包人工补传文件功能：hub_files 表新增 supplement_index JSON 列，
登记人工上传补充进包的文件索引（物理落盘 uploads/hub_supplements/{file_id}/，
读取面与 file_index 叠加展示，条目打 is_manual 标记）。

Revision ID: b7d2e8a4c651
Revises: a9e5f2c74d81
Create Date: 2026-10-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7d2e8a4c651'
down_revision: Union[str, None] = 'a9e5f2c74d81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('hub_files', sa.Column(
        'supplement_index', sa.JSON(), nullable=True,
        comment='人工补传文件索引（JSON 数组，读取时叠加进树/匹配视图）'))


def downgrade() -> None:
    op.drop_column('hub_files', 'supplement_index')
