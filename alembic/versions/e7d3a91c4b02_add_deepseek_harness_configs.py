"""add deepseek_harness_configs

Revision ID: e7d3a91c4b02
Revises: b7d21c9a4e50
Create Date: 2026-10-05 10:00:00.000000

DeepSeek Harness 全局配置表（单例激活，形态对齐 claude_code_configs）：
DeepSeek Agent 形态裁判 / deepseek_agent 类脚本执行器共用的
deepseek-harness-sdk 连接参数。设计：docs/design/pre-judge-scripts.md 第九节。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e7d3a91c4b02'
down_revision: Union[str, None] = 'b7d21c9a4e50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deepseek_harness_configs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False, server_default='默认配置', comment='配置名称'),
        sa.Column('base_url', sa.String(), nullable=True, comment='DeepSeek API 地址（OpenAI 兼容；空则用 SDK 默认/环境变量）'),
        sa.Column('api_key', sa.String(), nullable=True, comment='DEEPSEEK_API_KEY（空则用 SDK 默认/环境变量）'),
        sa.Column('provider', sa.String(), nullable=False, server_default='deepseek-official', comment='DSH provider 标识'),
        sa.Column('model', sa.String(), nullable=False, server_default='deepseek-v4-flash', comment='模型名'),
        sa.Column('profile', sa.String(), nullable=False, server_default='sdk-minimal', comment='DSH profile（默认 sdk-minimal，danger-full-access，须隔离运行）'),
        sa.Column('timeout_seconds', sa.Integer(), server_default='300', comment='单次裁判调用超时（秒）'),
        sa.Column('max_tokens', sa.Integer(), server_default='49152', comment='单次生成最大 token'),
        sa.Column('extra_env', sa.JSON(), nullable=True, comment='附加环境变量（JSON 对象，注入 harness 子进程）'),
        sa.Column('extra_patches', sa.JSON(), nullable=True, comment='附加 patch（JSON 数组，随 SDK 启动 patches 传入）'),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), comment='是否激活（单例）'),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_deepseek_harness_configs_id', 'deepseek_harness_configs', ['id'])


def downgrade() -> None:
    op.drop_index('ix_deepseek_harness_configs_id', table_name='deepseek_harness_configs')
    op.drop_table('deepseek_harness_configs')
