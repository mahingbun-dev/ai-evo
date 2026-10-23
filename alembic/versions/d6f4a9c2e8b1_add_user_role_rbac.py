"""M4 QE4-3: RBAC 三角色——users.role 列 + 存量回填

- users.role：团队角色 owner/engineer/viewer（可空，NULL 按 engineer 兜底，
  access_control.user_role 统一口径）；
- 存量回填：先统一 engineer（写权限与存量行为一致，不降权不破坏），再按
  「每团队（org_code 空=DEFAULT 组）最早成员」升 owner——团队创建者不可考
  （teams 无 created_by，成员关系即 users.org_code），按最早注册确定性回填，
  保证每支团队至少一位 owner 可自助管理角色；super_admin 不依赖本列（全局
  保留）。项目级细粒度 RBAC 不做（留 M6 企业版功能位）。

expand-only：可空列 + UPDATE 回填，不破坏存量任务与数据；downgrade 仅删列。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M4 QE4-3。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd6f4a9c2e8b1'
down_revision = 'c3f8b2d5e4a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NULL"
    )
    op.execute(
        "COMMENT ON COLUMN users.role IS "
        "'团队角色（M4 QE4-3）：owner/engineer/viewer，空按 engineer 兜底'"
    )
    # 1) 存量统一 engineer（默认角色；写权限与存量一致）
    op.execute("UPDATE users SET role = 'engineer' WHERE role IS NULL")
    # 2) 每团队（org_code 空=DEFAULT 组）最早成员升 owner（幂等：仅回填行）
    op.execute(
        """
        UPDATE users SET role = 'owner'
        WHERE user_code IN (
            SELECT user_code FROM (
                SELECT user_code,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(org_code, '')
                           ORDER BY created_at ASC NULLS FIRST, user_code
                       ) AS rn
                FROM users
                WHERE role = 'engineer'
            ) ranked
            WHERE ranked.rn = 1
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS role")
