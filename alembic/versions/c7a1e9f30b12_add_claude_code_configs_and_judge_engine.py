"""add claude_code_configs and judge_engine

Revision ID: c7a1e9f30b12
Revises: bf4dc0ecfd45
Create Date: 2026-10-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7a1e9f30b12'
down_revision: Union[str, None] = 'bf4dc0ecfd45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'claude_code_configs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False, server_default='默认配置', comment='配置名称'),
        sa.Column('base_url', sa.String(), nullable=False, comment='ANTHROPIC_BASE_URL，Anthropic 兼容网关地址'),
        sa.Column('auth_token', sa.String(), nullable=False, comment='ANTHROPIC_AUTH_TOKEN'),
        sa.Column('model', sa.String(), nullable=False, comment='模型名（ANTHROPIC_MODEL）'),
        sa.Column('timeout_seconds', sa.Integer(), server_default='300', comment='单次裁判调用超时（秒）'),
        sa.Column('max_turns', sa.Integer(), server_default='30', comment='Agent 裁判最大轮次'),
        sa.Column('proxy_url', sa.String(), nullable=True, comment='HTTPS_PROXY 代理地址（可选）'),
        sa.Column('extra_env', sa.JSON(), nullable=True, comment='附加环境变量（JSON 对象）'),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), comment='是否激活（单例）'),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_claude_code_configs_id', 'claude_code_configs', ['id'])

    op.add_column('evaluation_tasks', sa.Column(
        'judge_engine', sa.String(),
        server_default='claude_agent',
        nullable=True,
        comment='裁判引擎：claude_agent(Claude Code Agent)/graph(裁判图 HTTP 直调)，历史任务空值按 graph 处理',
    ))


def downgrade() -> None:
    op.drop_column('evaluation_tasks', 'judge_engine')
    op.drop_index('ix_claude_code_configs_id', table_name='claude_code_configs')
    op.drop_table('claude_code_configs')
