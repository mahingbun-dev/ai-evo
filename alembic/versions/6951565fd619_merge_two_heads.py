"""merge two heads

Revision ID: 6951565fd619
Revises: 0a66a8f097df, 1a2b3c4d5e6f
Create Date: 2026-04-14 19:35:59.463152

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6951565fd619'
down_revision: Union[str, None] = ('0a66a8f097df', '1a2b3c4d5e6f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
