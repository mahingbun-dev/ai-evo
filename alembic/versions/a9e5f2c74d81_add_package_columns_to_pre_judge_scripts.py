"""add package columns to pre_judge_scripts

数据集迭代（阶段E：脚本上传校验与脚手架）：
- package_dir：zip 脚本包解包目录（包根含 main.py，entrypoint 指包内 main.py）
- declared_requirements：依赖声明（JSON 数组，上传时逐包校验执行 venv）
- risk_acknowledged：风险声明文本（AST 风险扫描命中时必填，否则拒存）

Revision ID: a9e5f2c74d81
Revises: d5a9c3e74b16
Create Date: 2026-10-08 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a9e5f2c74d81'
down_revision: Union[str, None] = 'd5a9c3e74b16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pre_judge_scripts', sa.Column(
        'package_dir', sa.Text(), nullable=True,
        comment='脚本包解包目录（zip 包形态，包根必须含 main.py；单文件形态为空）'))
    op.add_column('pre_judge_scripts', sa.Column(
        'declared_requirements', sa.JSON(), nullable=True,
        comment='依赖声明（JSON 数组，逐包校验执行 venv，平台不自动安装）'))
    op.add_column('pre_judge_scripts', sa.Column(
        'risk_acknowledged', sa.Text(), nullable=True,
        comment='风险声明文本（AST 风险扫描命中时必填，否则拒存）'))


def downgrade() -> None:
    op.drop_column('pre_judge_scripts', 'risk_acknowledged')
    op.drop_column('pre_judge_scripts', 'declared_requirements')
    op.drop_column('pre_judge_scripts', 'package_dir')
