"""M3 QE3-1: safety 类型放行——evaluation_tasks.safety_config

- evaluation_tasks.safety_config：安全评测配置（攻击面类目多选、样本来源
  pack/dataset、内置样本库+版本快照、变异增强开关、双通道开关），可空；
- 类型白名单放行（safety）与红队引擎在应用层实现，无表结构变更。

expand-only：可空列，不破坏存量任务；downgrade 仅回退本次新增列。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M3 QE3-1。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a1f5e9c3d7b2'
down_revision = 'd4a7c9e3f5b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS "
        "safety_config JSONB NULL"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.safety_config IS "
        "'安全评测配置（M3 QE3-1）：{categories, sample_source, pack, pack_version, mutation, channels}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS safety_config")
