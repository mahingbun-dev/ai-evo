"""add script_id to hub_import_jobs

导入时可选挂载脚本库清洗脚本：hub_import_jobs 加 script_id 可空列——
非空时 worker 逐包执行清洗脚本，脚本产出的行内容作为数据集行数据落库；
为空保持原样导入（兼容既有任务与调用方）。

Revision ID: e4f7b1a9c2d8
Revises: b3f8a1c2d4e7
Create Date: 2026-10-13 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4f7b1a9c2d8'
down_revision: Union[str, None] = 'b3f8a1c2d4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'hub_import_jobs',
        sa.Column('script_id', sa.String(), nullable=True,
                  comment='清洗脚本 ID（脚本库，可空=不清洗）'),
    )


def downgrade() -> None:
    op.drop_column('hub_import_jobs', 'script_id')
