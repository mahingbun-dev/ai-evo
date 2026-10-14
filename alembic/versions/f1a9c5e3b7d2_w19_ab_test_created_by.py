"""ab_test_sessions 加 created_by 归属列（W19 权限补收口）

W9 IDOR 收口未覆盖 A/B 测试会话：任何登录用户可删除他人的测试记录。
本迁移为 ab_test_sessions 补 created_by（创建人 user_code）：
- expand-only、幂等（ADD COLUMN IF NOT EXISTS）；
- 历史行保持 NULL——读对所有登录用户可见，删除仅创建人或超管
  （与 app/api/ab_test_api.py 的读写口径配套）。

Revision ID: f1a9c5e3b7d2
Revises: a1b2c3d4e5f6
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a9c5e3b7d2'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE ab_test_sessions ADD COLUMN IF NOT EXISTS created_by VARCHAR")
    op.execute(
        "COMMENT ON COLUMN ab_test_sessions.created_by IS "
        "'创建人 user_code（W19；历史行为 NULL，读可见、删仅创建人/超管）'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ab_test_sessions DROP COLUMN IF EXISTS created_by")
