"""add enable_script_mcp to claude_code_configs

Revision ID: b8e1f5c3a9d7
Revises: e7d3a91c4b02
Create Date: 2026-10-05 12:00:00.000000

脚本 MCP 工具化开关（docs/design/pre-judge-scripts.md 第九节 9.3）：
开启后 ClaudeCodeAdapter 以 stdio MCP server 形态把全部 is_active 的
builtin 前处理脚本注入 claude-agent-sdk（ClaudeAgentOptions.mcp_servers），
裁判 agent 可在循环内直接调用确定性工具。默认关闭，行为不变。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8e1f5c3a9d7'
down_revision: Union[str, None] = 'e7d3a91c4b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'claude_code_configs',
        sa.Column(
            'enable_script_mcp', sa.Boolean(), nullable=False,
            server_default=sa.text('false'),
            comment='是否把内置前处理脚本以 stdio MCP server 注入 claude-agent-sdk（默认关闭）',
        ),
    )


def downgrade() -> None:
    op.drop_column('claude_code_configs', 'enable_script_mcp')
