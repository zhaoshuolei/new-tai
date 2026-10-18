"""contact follow ups

Revision ID: c375c8de4899
Revises: 26a11cef913c
Create Date: 2026-10-18 14:14:44.716362
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "c375c8de4899"
down_revision: str | Sequence[str] | None = "26a11cef913c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_follow_up_method",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DELETED", name="contact_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "active_name",
            sa.String(length=64),
            sa.Computed("CASE WHEN status = 'ACTIVE' THEN name END", persisted=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_name",
            name="uq_contact_follow_up_method_active_name",
        ),
    )
    op.create_table(
        "contact_follow_up",
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("method_id", sa.BigInteger(), nullable=False),
        sa.Column("follow_up_user_id", sa.BigInteger(), nullable=False),
        sa.Column("follow_up_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("next_follow_up_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DELETED", name="contact_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
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
            ["contact_id"],
            ["customer_contact.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_id"],
            ["contact_follow_up_method.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["follow_up_user_id"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_contact_follow_up_contact_id",
        "contact_follow_up",
        ["contact_id"],
        unique=False,
    )
    op.create_index(
        "ix_contact_follow_up_method_id",
        "contact_follow_up",
        ["method_id"],
        unique=False,
    )
    op.create_index(
        "ix_contact_follow_up_follow_up_user_id",
        "contact_follow_up",
        ["follow_up_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_contact_follow_up_contact_status_time",
        "contact_follow_up",
        ["contact_id", "status", "follow_up_at"],
        unique=False,
    )
    op.create_index(
        "ix_contact_follow_up_status_next",
        "contact_follow_up",
        ["status", "next_follow_up_at"],
        unique=False,
    )
    op.create_table(
        "contact_follow_up_attachment",
        sa.Column("follow_up_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
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
            ["follow_up_id"],
            ["contact_follow_up.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path"),
    )
    op.create_index(
        "ix_contact_follow_up_attachment_follow_up_id",
        "contact_follow_up_attachment",
        ["follow_up_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("contact_follow_up_attachment")
    op.drop_table("contact_follow_up")
    op.drop_table("contact_follow_up_method")
