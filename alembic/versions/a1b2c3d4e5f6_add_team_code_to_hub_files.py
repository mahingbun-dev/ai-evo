"""add hub_files.team_code (W9 对象级归属校验 IDOR 收口)

W9：hub_files 表原本无团队概念——任意团队用户可对任意执行包做
预览/下载/补传。本迁移为 hub_files 增加 team_code 列（导入时从目标
数据集 team_code 继承写入，历史行为 NULL、读取面按 DEFAULT 口径
参与归属比较）。

expand-only：upgrade 只加列（IF NOT EXISTS 幂等）；downgrade 仅回退
本次新增列。

Revision ID: a1b2c3d4e5f6
Revises: c4e8f9a2b7d6
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c4e8f9a2b7d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 团队归属（导入时从数据集继承写入）：可空 + 字面 'DEFAULT' 服务端默认，
    # 与 evaluation_tasks/evaluation_datasets/media_resources 的 team_code 口径一致。
    # PG 方言：ADD COLUMN IF NOT EXISTS 幂等 + COMMENT ON 独立语句
    op.execute(
        "ALTER TABLE hub_files ADD COLUMN IF NOT EXISTS team_code "
        "VARCHAR NULL DEFAULT 'DEFAULT'"
    )
    op.execute(
        "COMMENT ON COLUMN hub_files.team_code IS "
        "'所属团队代码（导入时从数据集继承）'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE hub_files DROP COLUMN IF EXISTS team_code")
