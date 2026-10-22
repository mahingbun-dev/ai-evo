"""add_plugin_marketplaces

Revision ID: d5e8f2a4c6b9
Revises: b3c9d7e2f1a4
Create Date: 2026-10-14

插件市场表（插件评测从 git 市场仓库选择评测对象）。
幂等式（仓库 expand-only 惯例）：PG CREATE TABLE IF NOT EXISTS。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd5e8f2a4c6b9'
down_revision: Union[str, None] = 'b3c9d7e2f1a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_marketplaces (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            git_url VARCHAR(500) NOT NULL,
            branch VARCHAR(100),
            local_path VARCHAR(500),
            sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            sync_error TEXT,
            last_synced_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_plugin_marketplaces_git_url ON plugin_marketplaces (git_url)"
    )
    op.execute(
        "COMMENT ON TABLE plugin_marketplaces IS '插件市场（git 仓库克隆，插件评测评测对象来源）'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plugin_marketplaces")
