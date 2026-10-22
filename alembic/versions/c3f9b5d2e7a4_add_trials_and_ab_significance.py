"""M2 QE2-3: judge 行级多 trials 列 + A/B 显著性统计列

Revision ID: c3f9b5d2e7a4
Revises: b0d6a4f8c2e1
Create Date: 2026-10-23

- evaluation_tasks.trials：judge 类型行级多 trial 次数（2-5，空/1=不开启，
  存量任务行为不变；语义对齐 τ-bench pass^k，见 app/services/pass_k.py）；
- ab_test_sessions.significance：多轮 A/B 的显著性统计（Wilcoxon/配对 t/
  Cliff's delta/McNemar，见 app/services/ab_statistics.py），单轮/旧记录 NULL。
两列均可空、可默认，expand-only，不破坏存量任务与数据。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c3f9b5d2e7a4"
down_revision = "b0d6a4f8c2e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS trials INTEGER NULL"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.trials IS "
        "'行级多 trial 次数（M2，judge）：2-5，空/1=不开启'"
    )
    op.execute(
        "ALTER TABLE ab_test_sessions ADD COLUMN IF NOT EXISTS significance JSONB NULL"
    )
    op.execute(
        "COMMENT ON COLUMN ab_test_sessions.significance IS "
        "'显著性统计（M2）：{duration_paired, success_mcnemar, rounds}，单轮为NULL'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ab_test_sessions DROP COLUMN IF EXISTS significance")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS trials")
