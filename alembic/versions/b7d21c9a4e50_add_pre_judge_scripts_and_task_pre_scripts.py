"""add pre_judge_scripts and evaluation_tasks.pre_scripts

评测前处理脚本机制（docs/design/pre-judge-scripts.md 第六节）：
- 新表 pre_judge_scripts：可插拔脚本注册表
- evaluation_tasks 加列 pre_scripts：任务级脚本编排（JSON）

Revision ID: b7d21c9a4e50
Revises: c7a1e9f30b12
Create Date: 2026-10-05 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7d21c9a4e50'
down_revision: Union[str, None] = 'c7a1e9f30b12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pre_judge_scripts',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False, comment='脚本名称（全局唯一，builtin 与任务编排均按名称引用）'),
        sa.Column('description', sa.Text(), nullable=True, comment='脚本用途说明'),
        sa.Column('exec_type', sa.String(), nullable=False, comment='执行类型：builtin/script/claude_agent/deepseek_agent'),
        sa.Column('entrypoint', sa.Text(), nullable=True, comment='builtin: 模块.函数名；script: 脚本路径；agent 类型: 指令文本'),
        sa.Column('allowed_tools', sa.JSON(), nullable=True, comment='CC 类型工具白名单（JSON 数组）'),
        sa.Column('harness_extra', sa.JSON(), nullable=True, comment='DSH 专用：{provider, model, profile, patches, max_tokens, reasoning_effort}'),
        sa.Column('input_fields', sa.JSON(), nullable=True, comment='声明所需输入字段：{名: {value_type: inline/local/oss/hub_file, description}}'),
        sa.Column('output_schema', sa.JSON(), nullable=True, comment='描述 output 键（JSON，驱动前端变量选择）'),
        sa.Column('timeout_seconds', sa.Integer(), server_default='120', nullable=False, comment='单次执行超时（秒）'),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), comment='是否启用（停用=软删除，历史任务引用仍可追溯）'),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('name', name='uq_pre_judge_scripts_name'),
    )
    op.create_index('ix_pre_judge_scripts_id', 'pre_judge_scripts', ['id'])

    op.add_column('evaluation_tasks', sa.Column(
        'pre_scripts', sa.JSON(),
        nullable=True,
        comment='评测前处理脚本编排列表（串行执行，输出进入裁判变量体系 script_output）',
    ))


def downgrade() -> None:
    op.drop_column('evaluation_tasks', 'pre_scripts')
    op.drop_index('ix_pre_judge_scripts_id', table_name='pre_judge_scripts')
    op.drop_table('pre_judge_scripts')
