"""create user profile

Revision ID: 26a11cef913c
Revises: 619b22fb0301
Create Date: 2026-10-18 02:47:39.663146
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "26a11cef913c"
down_revision: str | Sequence[str] | None = "619b22fb0301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_profile",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("signature", sa.String(length=100), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("work_phone", sa.String(length=32), nullable=True),
        sa.Column("extension", sa.String(length=16), nullable=True),
        sa.Column("work_email", sa.String(length=255), nullable=True),
        sa.Column("office_location", sa.String(length=100), nullable=True),
        sa.Column("seat", sa.String(length=50), nullable=True),
        sa.Column("expertise_tags", sa.JSON(), nullable=False),
        sa.Column("personal_links", sa.JSON(), nullable=False),
        sa.Column("hidden_public_fields", sa.JSON(), nullable=False),
        sa.Column("personal_phone", sa.String(length=32), nullable=True),
        sa.Column("birthday", sa.Date(), nullable=True),
        sa.Column("emergency_contact_name", sa.String(length=50), nullable=True),
        sa.Column("emergency_contact_phone", sa.String(length=32), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_user_profile_user_id", "user_id", unique=True),
    )


def downgrade() -> None:
    op.drop_table("user_profile")
