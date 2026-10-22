"""create ab_test_sessions if missing

Revision ID: a7c3e91b5d24
Revises: a1b2c3d4e5f6
Create Date: 2026-10-15

补建 ab_test_sessions 表。历史窟窿：该表从未进过迁移链（共享 dev 库里的表
是早期手工/create_all 建的），导致空库全量升级到 W19 迁移 f1a9c5e3b7d2
（默认表已存在并加列）时炸 UndefinedTable——check-migrations 门禁 2026-10-15
实测发现。按 app/models/ab_test_models.py 的声明补 DDL，幂等
（CREATE TABLE IF NOT EXISTS）：共享库已有该表时执行为无操作，仅形态对齐。

字段类型对照：String→VARCHAR、JSON→JSON、Float→DOUBLE PRECISION、
DateTime→TIMESTAMP（无时区）。status 的 default 是 Python 侧默认值，
不落 DDL DEFAULT（与 autogenerate 产出一致）。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91b5d24'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 注意：不含 created_by——该列归 W19 迁移 f1a9c5e3b7d2 所有（加列+注释），
    # 这里越权 COMMENT 会在「表已存在但尚无该列」的环境（如测试基线、
    # 停在 pre-W19 版本的环境）炸 UndefinedColumn。
    op.execute("""
        CREATE TABLE IF NOT EXISTS ab_test_sessions (
            id VARCHAR PRIMARY KEY,
            name VARCHAR,
            model_a_id VARCHAR NOT NULL,
            model_b_id VARCHAR NOT NULL,
            model_a_name VARCHAR,
            model_b_name VARCHAR,
            variables JSON,
            result_a JSON,
            result_b JSON,
            status VARCHAR,
            duration_a DOUBLE PRECISION,
            duration_b DOUBLE PRECISION,
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    op.execute(
        "COMMENT ON TABLE ab_test_sessions IS 'A/B 测试会话记录表'"
    )
    for column, comment in [
        ('id', '主键，格式：abtest_{uuid12}'),
        ('name', '可选备注名'),
        ('model_a_id', '模型A的配置ID'),
        ('model_b_id', '模型B的配置ID'),
        ('model_a_name', '模型A名称（冗余）'),
        ('model_b_name', '模型B名称（冗余）'),
        ('variables', '共享变量配置'),
        ('result_a', '模型A的完整执行结果'),
        ('result_b', '模型B的完整执行结果'),
        ('status', '状态：completed/failed'),
        ('duration_a', '模型A耗时（秒）'),
        ('duration_b', '模型B耗时（秒）'),
        ('created_at', '创建时间'),
    ]:
        op.execute(
            f"COMMENT ON COLUMN ab_test_sessions.{column} IS '{comment}'"
        )


def downgrade() -> None:
    # 表在共享库先于迁移存在，删除交给人工（expand-only 惯例：不自动 drop）
    pass
