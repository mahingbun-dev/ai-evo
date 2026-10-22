"""add gate_config/gate_result columns (M1 QE1-2)

Revision ID: e9c4d7a2f5b8
Revises: c3e8f2a1d9b4
Create Date: 2026-10-23

M1 门禁闭环（plans/2026-10-22-Agent质量工程平台开发方案.md §M1「门禁语义」）：
- evaluation_tasks.gate_config JSONB（可空）：{enabled, mode: all|any,
  thresholds: [{metric, op, value}]}；空 = 未启用门禁，存量任务行为不变；
- task_execution_records.gate_result JSONB（可空）：{verdict, checks,
  evaluated_at}；失败/停止收尾路径统一落 error verdict。

expand-only：仅加可空列（ADD COLUMN IF NOT EXISTS 幂等写法，与
a7c2f9e4b1d3 同款）；downgrade 仅回退本次新增列。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e9c4d7a2f5b8'
down_revision: Union[str, None] = 'c3e8f2a1d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS gate_config JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.gate_config IS "
        "'门禁配置（M1）：{enabled, mode: all|any, thresholds:[{metric,op,value}]},空=未启用'"
    )
    op.execute(
        "ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS gate_result JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN task_execution_records.gate_result IS "
        "'门禁判定（M1）：{verdict: passed|failed|error, checks:[...], evaluated_at},空=未启用门禁'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS gate_result")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS gate_config")
