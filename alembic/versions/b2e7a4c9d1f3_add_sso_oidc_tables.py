"""M4 QE4-1: OIDC SSO——sso_oidc_configs + sso_team_mappings

- sso_oidc_configs：IdP 配置单例（issuer/client_id/client_secret/scopes/
  claims 映射/redirect_uri/enabled），超管开发者设置页维护；
- sso_team_mappings：IdP groups claim → 平台 team_code 映射表，登录时
  首个命中（id 升序）生效并即时 upsert 团队。

expand-only：纯新增表，不触碰存量任务与数据；downgrade 仅回退本次新增表。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M4 QE4-1。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b2e7a4c9d1f3'
down_revision = 'a1f5e9c3d7b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sso_oidc_configs (
            id SERIAL PRIMARY KEY,
            issuer VARCHAR(500) NOT NULL,
            client_id VARCHAR(200) NOT NULL,
            client_secret VARCHAR(500) NOT NULL,
            scopes VARCHAR(300) NOT NULL DEFAULT 'openid profile email',
            claim_email VARCHAR(100) NOT NULL DEFAULT 'email',
            claim_username VARCHAR(100) NOT NULL DEFAULT 'preferred_username',
            claim_name VARCHAR(100) NOT NULL DEFAULT 'name',
            claim_groups VARCHAR(100) NOT NULL DEFAULT 'groups',
            redirect_uri VARCHAR(500),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_by VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE sso_oidc_configs IS "
        "'OIDC IdP 配置（M4 QE4-1，单例：服务层按 enabled 最新一条读写）'"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sso_team_mappings (
            id SERIAL PRIMARY KEY,
            idp_group VARCHAR(200) NOT NULL,
            team_code VARCHAR(100) NOT NULL,
            team_name VARCHAR(200),
            created_by VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE sso_team_mappings IS "
        "'IdP group→平台团队映射（M4 QE4-1，登录时 id 升序首个命中）'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_sso_team_mappings_idp_group "
        "ON sso_team_mappings (idp_group)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sso_team_mappings")
    op.execute("DROP TABLE IF EXISTS sso_oidc_configs")
