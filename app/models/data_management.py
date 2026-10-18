"""Custom data management persistence models."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.mysql import DATETIME, DECIMAL
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DataTableStatus(StrEnum):
    DRAFT = "DRAFT"
    LOCKED = "LOCKED"


class DataFieldType(StrEnum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"
    CUSTOMER = "CUSTOMER"


class DataFieldRole(StrEnum):
    BUSINESS_DATE = "BUSINESS_DATE"
    UPSTREAM_CUSTOMER = "UPSTREAM_CUSTOMER"
    DOWNSTREAM_CUSTOMER = "DOWNSTREAM_CUSTOMER"
    REVENUE = "REVENUE"
    COST = "COST"
    CUSTOM = "CUSTOM"


class DataImportSource(StrEnum):
    EXCEL = "EXCEL"
    API = "API"


class DataImportStatus(StrEnum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    APPLYING = "APPLYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DataSnapshotStatus(StrEnum):
    STAGED = "STAGED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class DataTableDefinition(Base):
    """A logical data table whose fixed and custom fields are metadata rows."""

    __tablename__ = "custom_data_table"
    __table_args__ = (UniqueConstraint("name", name="uq_custom_data_table_name"),)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[DataTableStatus] = mapped_column(
        Enum(
            DataTableStatus,
            name="data_table_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=DataTableStatus.DRAFT,
        server_default=DataTableStatus.DRAFT.value,
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )


class DataFieldDefinition(Base):
    """A fixed system role or user-defined field in a data table."""

    __tablename__ = "custom_data_field"
    __table_args__ = (
        Index(
            "uq_custom_data_field_table_system_key",
            "table_id",
            "system_key_unique",
            unique=True,
        ),
        Index(
            "uq_custom_data_field_table_label",
            "table_id",
            "label",
            unique=True,
        ),
    )

    table_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_table.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    field_type: Mapped[DataFieldType] = mapped_column(
        Enum(
            DataFieldType,
            name="data_field_type",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    system_key: Mapped[DataFieldRole] = mapped_column(
        Enum(
            DataFieldRole,
            name="data_field_role",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    system_key_unique: Mapped[str | None] = mapped_column(
        String(32),
        Computed(
            "CASE WHEN system_key = 'CUSTOM' THEN NULL ELSE system_key END",
            persisted=True,
        ),
        nullable=True,
    )


class DataEnumOption(Base):
    """An allowed label for an ENUM field."""

    __tablename__ = "custom_data_enum_option"
    __table_args__ = (
        UniqueConstraint(
            "field_id",
            "label",
            name="uq_custom_data_enum_option_field_label",
        ),
    )

    field_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_field.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )


class DataImportJob(Base):
    """A durable Excel or API import request and worker lease."""

    __tablename__ = "custom_data_import"
    __table_args__ = (
        Index(
            "ix_custom_data_import_table_status",
            "table_id",
            "status",
        ),
        Index(
            "ix_custom_data_import_status_lease",
            "status",
            "lease_expires_at",
        ),
    )

    table_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_table.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[DataImportSource] = mapped_column(
        Enum(
            DataImportSource,
            name="data_import_source",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    status: Mapped[DataImportStatus] = mapped_column(
        Enum(
            DataImportStatus,
            name="data_import_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=DataImportStatus.QUEUED,
        server_default=DataImportStatus.QUEUED.value,
    )
    original_filename: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    total_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    valid_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    invalid_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    progress_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    queued_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=func.utc_timestamp(6),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )


class DataImportError(Base):
    """A bounded row-level diagnostic for a failed import."""

    __tablename__ = "custom_data_import_error"
    __table_args__ = (
        Index("ix_custom_data_import_error_import_id", "import_id"),
        Index("ix_custom_data_import_error_field_id", "field_id"),
    )

    import_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_import.id", ondelete="CASCADE"),
        nullable=False,
    )
    sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    field_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_field.id", ondelete="RESTRICT"),
        nullable=True,
    )
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)


class DataSnapshot(Base):
    """An immutable row set for one table and business date."""

    __tablename__ = "custom_data_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "table_id",
            "business_date",
            "version",
            name="uq_custom_data_snapshot_table_date_version",
        ),
        Index(
            "uq_custom_data_snapshot_table_active_date",
            "table_id",
            "active_business_key",
            unique=True,
        ),
        Index(
            "ix_custom_data_snapshot_table_status_date",
            "table_id",
            "status",
            "business_date",
        ),
        Index("ix_custom_data_snapshot_import_id", "import_id"),
    )

    table_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_table.id", ondelete="CASCADE"),
        nullable=False,
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    import_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_import.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DataSnapshotStatus] = mapped_column(
        Enum(
            DataSnapshotStatus,
            name="data_snapshot_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=DataSnapshotStatus.STAGED,
        server_default=DataSnapshotStatus.STAGED.value,
    )
    active_business_key: Mapped[date | None] = mapped_column(
        Date,
        Computed(
            "CASE WHEN status = 'ACTIVE' THEN business_date END",
            persisted=True,
        ),
        nullable=True,
    )
    row_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )


DataCurrentSnapshot = Table(
    "custom_data_current_snapshot",
    Base.metadata,
    Column(
        "table_id",
        BigInteger,
        ForeignKey("custom_data_table.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("business_date", Date, primary_key=True),
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey("custom_data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column(
        "updated_at",
        DATETIME(fsp=6),
        nullable=False,
        server_default=func.utc_timestamp(6),
        onupdate=func.utc_timestamp(6),
    ),
)


class DataRecord(Base):
    """A current or historical business row within an immutable snapshot."""

    __tablename__ = "custom_data_record"
    __table_args__ = (
        Index(
            "ix_custom_data_record_table_date",
            "table_id",
            "business_date",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_table.id", ondelete="CASCADE"),
        nullable=False,
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    upstream_customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    downstream_customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    upstream_customer_name_source: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    downstream_customer_name_source: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    revenue: Mapped[Decimal] = mapped_column(
        DECIMAL(30, 8),
        nullable=False,
    )
    cost: Mapped[Decimal] = mapped_column(
        DECIMAL(30, 8),
        nullable=False,
    )


class DataRecordValue(Base):
    """One typed custom-field value belonging to a data record."""

    __tablename__ = "custom_data_record_value"
    __table_args__ = (
        UniqueConstraint(
            "record_id",
            "field_id",
            name="uq_custom_data_record_value_record_field",
        ),
        Index("ix_custom_data_record_value_record_id", "record_id"),
        Index("ix_custom_data_record_value_field_id", "field_id"),
        Index(
            "ix_custom_data_record_value_record_text",
            "record_id",
            "text_value",
            mysql_length={"text_value": 191},
        ),
        Index(
            "ix_custom_data_record_value_record_integer",
            "record_id",
            "integer_value",
        ),
        Index(
            "ix_custom_data_record_value_record_decimal",
            "record_id",
            "decimal_value",
        ),
        Index(
            "ix_custom_data_record_value_record_date",
            "record_id",
            "date_value",
        ),
        Index(
            "ix_custom_data_record_value_record_boolean",
            "record_id",
            "boolean_value",
        ),
        Index(
            "ix_custom_data_record_value_record_enum",
            "record_id",
            "enum_option_id",
        ),
    )

    record_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_record.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_field.id", ondelete="RESTRICT"),
        nullable=False,
    )
    text_value: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    integer_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decimal_value: Mapped[Decimal | None] = mapped_column(
        DECIMAL(30, 8),
        nullable=True,
    )
    date_value: Mapped[date | None] = mapped_column(Date, nullable=True)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    enum_option_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("custom_data_enum_option.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


__all__: tuple[str, ...] = (
    "DataCurrentSnapshot",
    "DataEnumOption",
    "DataFieldDefinition",
    "DataFieldRole",
    "DataFieldType",
    "DataImportError",
    "DataImportJob",
    "DataImportSource",
    "DataImportStatus",
    "DataRecord",
    "DataRecordValue",
    "DataSnapshot",
    "DataSnapshotStatus",
    "DataTableDefinition",
    "DataTableStatus",
)
