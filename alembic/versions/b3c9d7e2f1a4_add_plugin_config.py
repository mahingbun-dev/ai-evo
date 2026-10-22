"""add_plugin_config

Revision ID: b3c9d7e2f1a4
Revises: f1a9c5e3b7d2
Create Date: 2026-10-14

插件评测（evaluation_type=plugin）配置列。
幂等式（仓库 expand-only 惯例）：PG ADD COLUMN IF NOT EXISTS，部分库已
手工/先行建列时重复执行不报错。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b3c9d7e2f1a4'
down_revision: Union[str, None] = 'f1a9c5e3b7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG 方言：ADD COLUMN IF NOT EXISTS 幂等 + COMMENT ON 独立语句
    # （与 a1b2c3d4e5f6 的 hub_files.team_code 同款写法）
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS plugin_config JSON"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.plugin_config IS "
        "'插件评测配置'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS plugin_config")
