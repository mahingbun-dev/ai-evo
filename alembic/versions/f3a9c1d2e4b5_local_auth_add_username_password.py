"""local auth: add username and password_hash to users

本地账号登录改造：users 表新增 username（唯一）与 password_hash 列，
支持用户名+密码注册/登录（首注册用户自动授予超级管理员）。

Revision ID: f3a9c1d2e4b5
Revises: b7d2e8a4c651
Create Date: 2026-10-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d2e4b5'
down_revision: Union[str, None] = 'b7d2e8a4c651'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column(
        'username', sa.String(), nullable=True, comment='登录用户名（本地账号）'))
    op.add_column('users', sa.Column(
        'password_hash', sa.String(), nullable=True,
        comment='密码哈希（PBKDF2，本地账号）'))
    op.create_index('ix_users_username', 'users', ['username'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_users_username', table_name='users')
    op.drop_column('users', 'password_hash')
    op.drop_column('users', 'username')
