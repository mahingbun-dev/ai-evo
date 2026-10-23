"""M4 QE4-2: LDAP/AD 只读认证——ldap_configs

- ldap_configs：LDAP/AD 配置单例（host/use_ssl/bind_dn/base_dn/user_filter/
  属性映射），超管开发者设置页维护；search+bind 只读认证（写同步不做）。

expand-only：纯新增表，不触碰存量任务与数据；downgrade 仅回退本次新增表。
groups→team 映射复用 QE4-1 的 sso_team_mappings（OIDC 与 LDAP 共用一张表）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M4 QE4-2。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c3f8b2d5e4a6'
down_revision = 'b2e7a4c9d1f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ldap_configs (
            id SERIAL PRIMARY KEY,
            host VARCHAR(300) NOT NULL,
            port INTEGER,
            use_ssl BOOLEAN NOT NULL DEFAULT FALSE,
            bind_dn VARCHAR(500),
            bind_password VARCHAR(500),
            base_dn VARCHAR(500) NOT NULL,
            user_filter VARCHAR(500) NOT NULL
                DEFAULT '(&(objectClass=person)(sAMAccountName={username}))',
            attr_username VARCHAR(100) NOT NULL DEFAULT 'sAMAccountName',
            attr_email VARCHAR(100) NOT NULL DEFAULT 'mail',
            attr_name VARCHAR(100) NOT NULL DEFAULT 'displayName',
            attr_groups VARCHAR(100) NOT NULL DEFAULT 'memberOf',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_by VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE ldap_configs IS "
        "'LDAP/AD 只读认证配置（M4 QE4-2，单例：search+bind，写同步不做）'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ldap_configs")
