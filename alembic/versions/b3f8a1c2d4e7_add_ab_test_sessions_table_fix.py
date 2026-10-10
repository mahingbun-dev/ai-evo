"""add ab_test_sessions table (真正建表)

162ec555ef24 文件名声称建 ab_test_sessions，实际内容是给 model_configs 加
role_description 列，导致全新安装的库缺这张表，A/B 对比保存 500。此迁移按
ABTestSession 模型补齐建表；已存在该表的环境（若手工建过）幂等跳过。

Revision ID: b3f8a1c2d4e7
Revises: f3a9c1d2e4b5
Create Date: 2026-10-10 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f8a1c2d4e7'
down_revision: Union[str, None] = 'f3a9c1d2e4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ab_test_sessions" in inspector.get_table_names():
        return
    op.create_table(
        'ab_test_sessions',
        sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：abtest_{uuid12}'),
        sa.Column('name', sa.String(), nullable=True, comment='可选备注名'),
        sa.Column('model_a_id', sa.String(), nullable=False, comment='模型A的配置ID'),
        sa.Column('model_b_id', sa.String(), nullable=False, comment='模型B的配置ID'),
        sa.Column('model_a_name', sa.String(), nullable=True, comment='模型A名称（冗余）'),
        sa.Column('model_b_name', sa.String(), nullable=True, comment='模型B名称（冗余）'),
        sa.Column('variables', sa.JSON(), nullable=True, comment='共享变量配置'),
        sa.Column('result_a', sa.JSON(), nullable=True, comment='模型A的完整执行结果'),
        sa.Column('result_b', sa.JSON(), nullable=True, comment='模型B的完整执行结果'),
        sa.Column('status', sa.String(), comment='状态：completed/failed'),
        sa.Column('duration_a', sa.Float(), nullable=True, comment='模型A耗时（秒）'),
        sa.Column('duration_b', sa.Float(), nullable=True, comment='模型B耗时（秒）'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), comment='创建时间'),
    )
    op.create_index('ix_ab_test_sessions_id', 'ab_test_sessions', ['id'])


def downgrade() -> None:
    op.drop_index('ix_ab_test_sessions_id', table_name='ab_test_sessions')
    op.drop_table('ab_test_sessions')
