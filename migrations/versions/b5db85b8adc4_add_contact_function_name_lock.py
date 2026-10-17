"""add contact function name lock

Revision ID: b5db85b8adc4
Revises: b46bfc5aafcd
Create Date: 2026-10-17 15:14:09.574156
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b5db85b8adc4"
down_revision: str | Sequence[str] | None = "b46bfc5aafcd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_function_name_lock",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    lock_table = sa.table(
        "contact_function_name_lock",
        sa.column("id", sa.SmallInteger()),
    )
    op.bulk_insert(lock_table, [{"id": 1}])


def downgrade() -> None:
    op.drop_table("contact_function_name_lock")
