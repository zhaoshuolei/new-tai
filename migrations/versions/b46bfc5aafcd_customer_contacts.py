"""customer contacts

Revision ID: b46bfc5aafcd
Revises: cf349e47acee
Create Date: 2026-10-17 13:18:56.291237
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b46bfc5aafcd"
down_revision: str | Sequence[str] | None = "cf349e47acee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_function",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=True),
        sa.Column("scope_key", sa.BigInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key", "active_name", name="uq_contact_function_scope_name"
        ),
    )
    op.create_table(
        "customer_contact",
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("function_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.String(length=128), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("home_address", sa.String(length=512), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DELETED", name="contact_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
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
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["function_id"], ["contact_function.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_customer_contact_customer_id"),
        "customer_contact",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_contact_function_id"),
        "customer_contact",
        ["function_id"],
        unique=False,
    )
    op.add_column(
        "customer",
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DELETED", name="contact_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
    )
    op.add_column(
        "customer",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column(
        "customer",
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "customer",
        sa.Column(
            "active_credit_code",
            sa.String(length=18),
            sa.Computed(
                "CASE WHEN status = 'ACTIVE' THEN credit_code END", persisted=True
            ),
            nullable=True,
        ),
    )
    op.drop_index(op.f("ix_customer_credit_code"), table_name="customer")
    op.create_index(
        op.f("ix_customer_active_credit_code"),
        "customer",
        ["active_credit_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("customer_contact")
    op.drop_table("contact_function")
    op.drop_index(op.f("ix_customer_active_credit_code"), table_name="customer")
    op.drop_column("customer", "active_credit_code")
    op.create_index(
        op.f("ix_customer_credit_code"),
        "customer",
        ["credit_code"],
        unique=True,
    )
    op.drop_column("customer", "deleted_by")
    op.drop_column("customer", "deleted_at")
    op.drop_column("customer", "status")
