"""add trajectory_config / trajectory_match columns (M2 QE2-2 轨迹匹配)

Revision ID: b0d6a4f8c2e1
Revises: a8c4e2f1b9d3
Create Date: 2026-10-23

M2 方法论补课（plans/2026-10-22-Agent质量工程平台开发方案.md §M2 2.2）：
- evaluation_tasks.trajectory_config：agent 类型任务的轨迹断言配置 JSONB
  （{mode: strict|unordered|subset|superset, expected[], forbidden[]}，
  空=未配置，存量任务行为不变）；
- evaluation_results.trajectory_match：行级轨迹匹配结果投影 JSONB
  （{mode, status, matched, missing, unexpected, forbidden_hits, timeline}，
  事实源仍是 row_result JSON 内同名字段）。

expand-only：两列均可空、带空缺省，ADD COLUMN IF NOT EXISTS 幂等，
不触碰存量任务与数据；downgrade 仅回退本次新增列。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b0d6a4f8c2e1'
down_revision: Union[str, None] = 'a8c4e2f1b9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS trajectory_config JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.trajectory_config IS "
        "'轨迹断言配置（M2，agent）：{mode, expected[], forbidden[]},空=未配置'"
    )
    op.execute(
        "ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS trajectory_match JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_results.trajectory_match IS "
        "'轨迹匹配结果（M2）：{mode, status, matched, missing, unexpected, forbidden_hits, timeline}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_results DROP COLUMN IF EXISTS trajectory_match")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS trajectory_config")
