"""add verifiers / verification_result columns (M2 QE2-1 终态校验器)

Revision ID: a8c4e2f1b9d3
Revises: b5a1e9c3f7d2
Create Date: 2026-10-23

M2 方法论补课（plans/2026-10-22-Agent质量工程平台开发方案.md §M2 2.1）：
- evaluation_tasks.verifiers：任务级终态校验器配置 JSONB（四种内置类型
  script/db_assert/file_assert/http_assert，空=未配置，存量任务行为不变）；
- evaluation_results.verification_result：行级校验结果投影 JSONB
  （事实源仍是 row_result JSON 内同名字段，本列供行级查询）。

expand-only：两列均可空、带空缺省，ADD COLUMN IF NOT EXISTS 幂等，
不触碰存量任务与数据；downgrade 仅回退本次新增列。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a8c4e2f1b9d3'
down_revision: Union[str, None] = 'b5a1e9c3f7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS verifiers JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.verifiers IS "
        "'终态校验器配置（M2）：[{type: script|db_assert|file_assert|http_assert, name, on_fail, config}],空=未配置'"
    )
    op.execute(
        "ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS verification_result JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_results.verification_result IS "
        "'终态校验器结果（M2）：{status: pass|fail|error, results:[...], passed, total}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_results DROP COLUMN IF EXISTS verification_result")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS verifiers")
