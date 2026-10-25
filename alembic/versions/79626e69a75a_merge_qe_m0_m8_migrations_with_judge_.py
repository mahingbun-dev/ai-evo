"""merge QE M0-M8 migrations with judge-pool

Revision ID: 79626e69a75a
Revises: b3e7c9f5d2a8, c7e3f1a9b2d4
Create Date: 2026-10-25 10:14:40.278906

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '79626e69a75a'
down_revision: Union[str, None] = ('b3e7c9f5d2a8', 'c7e3f1a9b2d4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
