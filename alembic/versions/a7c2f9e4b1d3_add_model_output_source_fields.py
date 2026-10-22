"""add model output source fields to evaluation_tasks

judge 评测支持「模型输出来自数据集」：
- model_output_source：model_call（默认，存量行为）/ dataset_field / none；
  server_default 'model_call'，存量任务未回填也按 model_call 语义执行；
- model_output_field：source=dataset_field 时的数据集字段名。

expand-only：upgrade 只加列；downgrade 仅回退本次新增对象（与
e9b4a2c7d5f8 同款 ADD COLUMN IF NOT EXISTS 幂等写法）。

Revision ID: a7c2f9e4b1d3
Revises: e9b4a2c7d5f8
Create Date: 2026-10-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c2f9e4b1d3'
down_revision: Union[str, None] = 'e9b4a2c7d5f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS "
        "model_output_source VARCHAR(32) DEFAULT 'model_call'"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.model_output_source IS "
        "'模型输出来源：model_call-调用被评测模型（默认）/dataset_field-数据集字段/none-不使用（空值按 model_call 处理）'"
    )
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS model_output_field VARCHAR"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.model_output_field IS "
        "'模型输出字段名（model_output_source=dataset_field 时生效）'"
    )


def downgrade() -> None:
    # 仅回退本次新增列（expand-only 回滚语义：本迁移引入的对象才可被移除）
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS model_output_field")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS model_output_source")
