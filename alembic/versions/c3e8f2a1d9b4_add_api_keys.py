"""add api_keys table (M1 QE1-1)

Revision ID: c3e8f2a1d9b4
Revises: a7c2f9e4b1d3
Create Date: 2026-10-23

M1 门禁闭环（plans/2026-10-22-Agent质量工程平台开发方案.md §M1 数据模型）：
API Key 表——CI/CLI 侧 Bearer aievo_sk_... 长期凭证。仅存 sha256 与随机段
前 8 位前缀；明文只在创建时返回一次。

幂等式（仓库 expand-only 惯例）：PG CREATE TABLE IF NOT EXISTS，纯新增表
不触碰存量任务与数据；downgrade 仅回退本次新增对象。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3e8f2a1d9b4'
down_revision: Union[str, None] = 'a7c2f9e4b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            user_code VARCHAR NOT NULL,
            team_code VARCHAR,
            name VARCHAR(100) NOT NULL,
            key_prefix VARCHAR(8) NOT NULL,
            key_hash VARCHAR(64) NOT NULL,
            scopes JSONB,
            last_used_at TIMESTAMP WITH TIME ZONE,
            expires_at TIMESTAMP WITH TIME ZONE,
            revoked_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    # 认证扫描索引：按 key_prefix 前缀单查询（方案指定名称 ix_api_keys_prefix）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_keys_prefix ON api_keys (key_prefix)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_keys_user_code ON api_keys (user_code)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys (key_hash)"
    )
    op.execute("COMMENT ON TABLE api_keys IS 'API Key（Bearer aievo_sk_... 长期凭证，M1 门禁闭环）'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_keys")
