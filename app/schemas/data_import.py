"""Data import request and response schemas."""

from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.data_management import DataImportSource, DataImportStatus


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class JsonImportRow(BaseModel):
    """One structured JSON data row before dynamic field validation."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    business_date: date
    upstream_customer_id: int
    downstream_customer_id: int
    revenue: Decimal
    cost: Decimal
    custom_values: dict[str, str | int | Decimal | bool | None] = Field(
        default_factory=dict
    )


class DataImportJsonRequest(BaseModel):
    """A bounded batch of structured JSON data rows."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    rows: list[JsonImportRow] = Field(max_length=100_000)


class DataImportRead(BaseModel):
    """Public import status without private storage details."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    table_id: int
    source: DataImportSource
    status: DataImportStatus
    original_filename: str | None
    size_bytes: int
    requested_by: int
    total_rows: int
    valid_rows: int
    invalid_rows: int
    date_from: date | None
    date_to: date | None
    error_count: int
    progress_percent: int
    failure_code: str | None
    failure_message: str | None
    attempt_count: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "queued_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class DataImportErrorRead(BaseModel):
    """One bounded, row-scoped import diagnostic."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    import_id: int
    sheet_name: str | None
    row_number: int | None
    column_label: str | None
    field_id: int | None
    error_code: str
    message: str
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str | None:
        return _serialize_utc(value)


__all__: tuple[str, ...] = (
    "DataImportErrorRead",
    "DataImportJsonRequest",
    "DataImportRead",
    "JsonImportRow",
)
