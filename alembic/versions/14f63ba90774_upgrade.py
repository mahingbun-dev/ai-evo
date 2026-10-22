"""upgrade

Revision ID: 14f63ba90774
Revises: 09f568a8759f
Create Date: 2026-04-09 20:17:15.119953

重复迁移：本文件与更早的 5a64da264cdd 加的是同一批列（evaluation_targets 等），
历史遗留。空库全量升级时 op.add_column 会因列已存在而炸（check-migrations
门禁实测发现），改为幂等写法（upgrade 探测已存在列跳过、downgrade 用
DROP COLUMN IF EXISTS）；已存在的环境（alembic_version 已越过本节点）行为不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '14f63ba90774'
down_revision: Union[str, None] = '09f568a8759f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (表, 列, 类型, 注释)；5a64da264cdd 已加过同名列，这里保持幂等
_COLUMNS = [
    ('evaluation_tasks', 'evaluation_targets', 'JSON', '评测目标配置列表'),
    ('evaluation_tasks', 'validation_enabled', 'BOOLEAN', '是否启用空值校验'),
    ('task_execution_records', 'judge_index', 'INTEGER', '裁判序号'),
    ('task_execution_records', 'evaluation_target_id', 'VARCHAR', '评测目标ID'),
    ('task_execution_records', 'evaluation_target_name', 'VARCHAR', '评测目标名称'),
    ('task_execution_records', 'validation_message', 'TEXT', '空值校验信息'),
]


def upgrade() -> None:
    # 本迁移与公共链上的 5a64da264cdd 重复（分叉后各自提交了同一批列）。
    # 全新库 upgrade head 时公共链先加好这些列，这里必须幂等跳过已存在的列，
    # 否则 ADD COLUMN 直接报 DuplicateColumn。
    existing = {
        table: {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for table in ("evaluation_tasks", "task_execution_records")
    }

    def add_if_missing(table, col):
        if col.name not in existing[table]:
            op.add_column(table, col)

    add_if_missing('evaluation_tasks', sa.Column('evaluation_targets', sa.JSON(), nullable=True, comment='评测目标配置列表'))
    add_if_missing('evaluation_tasks', sa.Column('validation_enabled', sa.Boolean(), nullable=True, comment='是否启用空值校验'))
    add_if_missing('task_execution_records', sa.Column('judge_index', sa.Integer(), nullable=True, comment='裁判序号'))
    add_if_missing('task_execution_records', sa.Column('evaluation_target_id', sa.String(), nullable=True, comment='评测目标ID'))
    add_if_missing('task_execution_records', sa.Column('evaluation_target_name', sa.String(), nullable=True, comment='评测目标名称'))
    add_if_missing('task_execution_records', sa.Column('validation_message', sa.Text(), nullable=True, comment='空值校验信息'))


def downgrade() -> None:
    for table, column, _, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
