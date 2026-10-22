"""M2 QE2-4: 裁判治理（judge_guard_disabled / judge_canary / judge_config_snapshot）

Revision ID: d4a7c9e3f5b8
Revises: c3f9b5d2e7a4
Create Date: 2026-10-23

- evaluation_tasks.judge_guard_disabled：裁判输入消毒显式关闭声明（默认
  false=开启消毒；true 时关闭 <eval_data> 定界与免责指令，审计可见）；
- evaluation_tasks.judge_canary：裁判金丝雀自检开关（默认 false；true 时
  混入约 2% 空/乱码探针行，高分则结果页标风险徽标，不计均分）；
- task_execution_records.judge_config_snapshot：本次裁决所用裁判配置快照
  （引擎/裁判模型摘要/prompt 模板/维度定义/消毒与金丝雀状态）。

expand-only：均可空/带默认，不破坏存量任务与执行记录；downgrade 仅回退
本次新增列。见 plans/2026-10-22-Agent质量工程平台开发方案.md §M2 2.4。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd4a7c9e3f5b8'
down_revision = 'c3f9b5d2e7a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS "
        "judge_guard_disabled BOOLEAN NULL DEFAULT false"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.judge_guard_disabled IS "
        "'裁判输入消毒显式关闭（M2）：true=关闭定界+免责指令，默认false=开启'"
    )
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS "
        "judge_canary BOOLEAN NULL DEFAULT false"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.judge_canary IS "
        "'裁判金丝雀自检（M2）：true=混入空/乱码探针行，默认false=关闭'"
    )
    op.execute(
        "ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS "
        "judge_config_snapshot JSONB NULL"
    )
    op.execute(
        "COMMENT ON COLUMN task_execution_records.judge_config_snapshot IS "
        "'裁判治理快照（M2）：{engine, judge_model, prompt_template, metrics, guard, canary}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS judge_config_snapshot")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS judge_canary")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS judge_guard_disabled")
