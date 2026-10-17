"""expand customer business scope

Revision ID: e91f4c2a7b6d
Revises: d7c1e4a9b2f6
Create Date: 2026-10-17 21:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "e91f4c2a7b6d"
down_revision: str | Sequence[str] | None = "d7c1e4a9b2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "customer",
        "business_scope",
        existing_type=sa.Text(),
        type_=mysql.LONGTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # TEXT cannot safely hold every value accepted by LONGTEXT.
    op.alter_column(
        "customer",
        "business_scope",
        existing_type=mysql.LONGTEXT(),
        type_=mysql.TEXT(),
        existing_nullable=True,
    )
