"""custom data management

Revision ID: 4d8e1f2a7c90
Revises: a7d3c9e15b42
Create Date: 2026-10-18 22:02:03.990551

Creates the metadata, durable import, immutable snapshot, and typed EAV value
tables. The migration intentionally performs no business-row DML.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "4d8e1f2a7c90"
down_revision: str | Sequence[str] | None = "a7d3c9e15b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_data_table",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "LOCKED", name="data_table_status"),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("locked_at", mysql.DATETIME(fsp=6), nullable=True),
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
            ["created_by"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_custom_data_table_name"),
    )
    op.create_table(
        "custom_data_field",
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column(
            "field_type",
            sa.Enum(
                "TEXT",
                "INTEGER",
                "DECIMAL",
                "DATE",
                "BOOLEAN",
                "ENUM",
                "CUSTOMER",
                name="data_field_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "system_key",
            sa.Enum(
                "BUSINESS_DATE",
                "UPSTREAM_CUSTOMER",
                "DOWNSTREAM_CUSTOMER",
                "REVENUE",
                "COST",
                "CUSTOM",
                name="data_field_role",
            ),
            nullable=False,
        ),
        sa.Column(
            "required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "system_key_unique",
            sa.String(length=32),
            sa.Computed(
                "CASE WHEN system_key = 'CUSTOM' THEN NULL ELSE system_key END",
                persisted=True,
            ),
            nullable=True,
        ),
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
            ["table_id"],
            ["custom_data_table.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_custom_data_field_table_label",
        "custom_data_field",
        ["table_id", "label"],
        unique=True,
    )
    op.create_index(
        "uq_custom_data_field_table_system_key",
        "custom_data_field",
        ["table_id", "system_key_unique"],
        unique=True,
    )
    op.create_table(
        "custom_data_enum_option",
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
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
            ["field_id"],
            ["custom_data_field.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "field_id",
            "label",
            name="uq_custom_data_enum_option_field_label",
        ),
    )
    op.create_table(
        "custom_data_import",
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("EXCEL", "API", name="data_import_source"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "VALIDATING",
                "APPLYING",
                "SUCCEEDED",
                "FAILED",
                name="data_import_status",
            ),
            server_default="QUEUED",
            nullable=False,
        ),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "total_rows",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "valid_rows",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "invalid_rows",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column(
            "error_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "progress_percent",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("heartbeat_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "queued_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
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
            ["requested_by"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["custom_data_table.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_data_import_requested_by",
        "custom_data_import",
        ["requested_by"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_import_status_lease",
        "custom_data_import",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_import_table_status",
        "custom_data_import",
        ["table_id", "status"],
        unique=False,
    )
    op.create_table(
        "custom_data_import_error",
        sa.Column("import_id", sa.BigInteger(), nullable=False),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("column_label", sa.String(length=100), nullable=True),
        sa.Column("field_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
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
            ["field_id"],
            ["custom_data_field.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["custom_data_import.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_data_import_error_field_id",
        "custom_data_import_error",
        ["field_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_import_error_import_id",
        "custom_data_import_error",
        ["import_id"],
        unique=False,
    )
    op.create_table(
        "custom_data_snapshot",
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "STAGED",
                "ACTIVE",
                "SUPERSEDED",
                name="data_snapshot_status",
            ),
            server_default="STAGED",
            nullable=False,
        ),
        sa.Column(
            "active_business_key",
            sa.Date(),
            sa.Computed(
                "CASE WHEN status = 'ACTIVE' THEN business_date END",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "row_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("activated_at", mysql.DATETIME(fsp=6), nullable=True),
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
            ["import_id"],
            ["custom_data_import.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["custom_data_table.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "table_id",
            "business_date",
            "version",
            name="uq_custom_data_snapshot_table_date_version",
        ),
    )
    op.create_index(
        "ix_custom_data_snapshot_import_id",
        "custom_data_snapshot",
        ["import_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_snapshot_table_status_date",
        "custom_data_snapshot",
        ["table_id", "status", "business_date"],
        unique=False,
    )
    op.create_index(
        "uq_custom_data_snapshot_table_active_date",
        "custom_data_snapshot",
        ["table_id", "active_business_key"],
        unique=True,
    )
    op.create_table(
        "custom_data_current_snapshot",
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["custom_data_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["custom_data_table.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("table_id", "business_date"),
    )
    op.create_index(
        "ix_custom_data_current_snapshot_snapshot_id",
        "custom_data_current_snapshot",
        ["snapshot_id"],
        unique=False,
    )
    op.create_table(
        "custom_data_record",
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("upstream_customer_id", sa.BigInteger(), nullable=False),
        sa.Column("downstream_customer_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "upstream_customer_name_source",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "downstream_customer_name_source",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column("revenue", mysql.DECIMAL(30, 8), nullable=False),
        sa.Column("cost", mysql.DECIMAL(30, 8), nullable=False),
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
            ["downstream_customer_id"],
            ["customer.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["custom_data_snapshot.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["custom_data_table.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_customer_id"],
            ["customer.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_data_record_downstream_customer_id",
        "custom_data_record",
        ["downstream_customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_snapshot_id",
        "custom_data_record",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_table_date",
        "custom_data_record",
        ["table_id", "business_date"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_upstream_customer_id",
        "custom_data_record",
        ["upstream_customer_id"],
        unique=False,
    )
    op.create_table(
        "custom_data_record_value",
        sa.Column("record_id", sa.BigInteger(), nullable=False),
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("text_value", sa.String(length=4000), nullable=True),
        sa.Column("integer_value", sa.BigInteger(), nullable=True),
        sa.Column("decimal_value", mysql.DECIMAL(30, 8), nullable=True),
        sa.Column("date_value", sa.Date(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("enum_option_id", sa.BigInteger(), nullable=True),
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
            ["enum_option_id"],
            ["custom_data_enum_option.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["custom_data_field.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["custom_data_record.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "record_id",
            "field_id",
            name="uq_custom_data_record_value_record_field",
        ),
    )
    op.create_index(
        "ix_custom_data_record_value_enum_option_id",
        "custom_data_record_value",
        ["enum_option_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_field_id",
        "custom_data_record_value",
        ["field_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_boolean",
        "custom_data_record_value",
        ["record_id", "boolean_value"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_date",
        "custom_data_record_value",
        ["record_id", "date_value"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_decimal",
        "custom_data_record_value",
        ["record_id", "decimal_value"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_enum",
        "custom_data_record_value",
        ["record_id", "enum_option_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_id",
        "custom_data_record_value",
        ["record_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_integer",
        "custom_data_record_value",
        ["record_id", "integer_value"],
        unique=False,
    )
    op.create_index(
        "ix_custom_data_record_value_record_text",
        "custom_data_record_value",
        ["record_id", "text_value"],
        unique=False,
        mysql_length={"text_value": 191},
    )


def downgrade() -> None:
    op.drop_table("custom_data_record_value")
    op.drop_table("custom_data_record")
    op.drop_table("custom_data_current_snapshot")
    op.drop_table("custom_data_snapshot")
    op.drop_table("custom_data_import_error")
    op.drop_table("custom_data_import")
    op.drop_table("custom_data_enum_option")
    op.drop_table("custom_data_field")
    op.drop_table("custom_data_table")
