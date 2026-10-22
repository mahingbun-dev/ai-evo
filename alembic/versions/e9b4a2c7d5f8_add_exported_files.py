"""add exported_files to task_execution_records（导出文档幂等登记）

插件 task_execution_export 工具（v1.3.0）：把指定执行版本的结果渲染成
markdown 全量报告 / json 无损文档并上传 anc-hub file-store，返回 file_id。
exported_files 按 format 登记 {file_id, filename, size, sha256, uploaded_at}，
同版本同格式重复导出直接复用已传 file_id，避免重复上传。

expand-only：upgrade 只加列；downgrade 仅回退本次新增对象（与
f8e1d4c7a2b6 同款 ADD COLUMN IF NOT EXISTS 幂等写法）。

Revision ID: e9b4a2c7d5f8
Revises: d5e8f2a4c6b9
Create Date: 2026-10-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e9b4a2c7d5f8'
down_revision: Union[str, None] = 'd5e8f2a4c6b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS exported_files JSONB"
    )
    # 导出登记是展示/复用辅助信息，注释随列走便于人工排障
    op.execute(
        "COMMENT ON COLUMN task_execution_records.exported_files IS "
        "'已上传 anc-hub file-store 的导出文档登记（按格式幂等复用 file_id）'"
    )


def downgrade() -> None:
    # 仅回退本次新增列（expand-only 回滚语义：本迁移引入的对象才可被移除）
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS exported_files")
