"""add hub_files table

anc-hub 执行包登记表（数据集迭代：anc-hub 执行包导入 + 脚本文件契约）：
- hub_files：file_id 唯一索引、解包产物路径/状态/索引、引用计数（归零清理）

Revision ID: c2f7a9d41e08
Revises: a3f8c2d61b04
Create Date: 2026-10-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c2f7a9d41e08'
down_revision: Union[str, None] = 'a3f8c2d61b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'hub_files',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('file_id', sa.String(), nullable=False, comment='anc-hub file-store 文件 ID（全局唯一）'),
        sa.Column('filename', sa.String(), nullable=True, comment='anc-hub 原始文件名'),
        sa.Column('size', sa.Integer(), nullable=True, comment='zip 包大小（字节）'),
        sa.Column('sha256', sa.String(), nullable=True, comment='zip 包 sha256'),
        sa.Column('uploader', sa.String(), nullable=True, comment='hub 上传者'),
        sa.Column('hub_created_at', sa.DateTime(timezone=True), nullable=True, comment='hub 侧上传时间'),
        sa.Column('unpack_dir', sa.String(), nullable=True, comment='解包目录（uploads/hub_datasets/{file_id}）'),
        sa.Column('unpack_status', sa.String(), nullable=False, server_default='pending',
                  comment='解包状态：pending|unpacked|failed'),
        sa.Column('unpack_error', sa.Text(), nullable=True, comment='最近一次解包失败的中文错误信息'),
        sa.Column('file_count', sa.Integer(), nullable=True, comment='解包文件数'),
        sa.Column('total_size', sa.Integer(), nullable=True, comment='解包后总字节数'),
        sa.Column('ref_count', sa.Integer(), nullable=False, server_default='0', comment='引用计数'),
        sa.Column('file_index', sa.JSON(), nullable=True, comment='解包文件索引（JSON 数组）'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_hub_files_id', 'hub_files', ['id'])
    # file_id 唯一索引：同一 hub 文件只登记一行（幂等导入的命中键）
    op.create_index('uq_hub_files_file_id', 'hub_files', ['file_id'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_hub_files_file_id', table_name='hub_files')
    op.drop_index('ix_hub_files_id', table_name='hub_files')
    op.drop_table('hub_files')
