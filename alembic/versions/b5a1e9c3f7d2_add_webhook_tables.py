"""add webhook_endpoints/webhook_deliveries tables (M1 QE1-3)

Revision ID: b5a1e9c3f7d2
Revises: e9c4d7a2f5b8
Create Date: 2026-10-23

M1 门禁闭环（plans/2026-10-22-Agent质量工程平台开发方案.md §M1「Webhook」）：
- webhook_endpoints：团队级接收端（url/secret/events/active/created_by）；
- webhook_deliveries：投递留痕（status/response_status/attempts/last_error）。

幂等式（仓库 expand-only 惯例）：PG CREATE TABLE IF NOT EXISTS；纯新增表，
不触碰存量任务与数据；downgrade 仅回退本次新增对象。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b5a1e9c3f7d2'
down_revision: Union[str, None] = 'e9c4d7a2f5b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_endpoints (
            id SERIAL PRIMARY KEY,
            team_code VARCHAR NOT NULL,
            name VARCHAR(100) NOT NULL,
            url VARCHAR(500) NOT NULL,
            secret VARCHAR NOT NULL,
            events JSONB,
            active BOOLEAN NOT NULL DEFAULT true,
            created_by VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_endpoints_team_code ON webhook_endpoints (team_code)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id SERIAL PRIMARY KEY,
            endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
            event VARCHAR(100) NOT NULL,
            payload JSONB,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            response_status INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_endpoint_id ON webhook_deliveries (endpoint_id)"
    )
    op.execute("COMMENT ON TABLE webhook_endpoints IS 'webhook 接收端（团队级，M1 门禁通知面）'")
    op.execute("COMMENT ON TABLE webhook_deliveries IS 'webhook 投递留痕（pending/success/failed）'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS webhook_endpoints")
