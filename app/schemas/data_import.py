"""Data import request and response schemas."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Never

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
)

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import DataImportSource, DataImportStatus


def _reject_binary_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("JSON decimals must not pass through binary float")
    return value


def _exact_decimal_input(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Decimal value must be a string, integer, or Decimal")
    return value


JsonDecimal = Annotated[Decimal, BeforeValidator(_exact_decimal_input)]
JsonCustomValue = Annotated[
    str | int | JsonDecimal | bool | None,
    BeforeValidator(_reject_binary_float),
]
StrictCustomerId = Annotated[int, Field(strict=True, gt=0)]


def _reject_json_constant(constant: str) -> Never:
    raise ValueError(f"Non-standard JSON constant: {constant}")


_INVALID_JSON_MESSAGE = "Import JSON is invalid"


def _invalid_json() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_INVALID,
        _INVALID_JSON_MESSAGE,
        http_status=422,
    )


def parse_data_import_json_request(content: bytes) -> DataImportJsonRequest:
    """Parse raw API JSON without allowing decimal numbers through binary float.

    Takes the **original request bytes** and returns a validated
    :class:`DataImportJsonRequest`. Numbers are decoded with
    ``parse_float=Decimal`` so digit strings, exponents, signs, and trailing
    zeros survive unchanged.

    Malformed or unsupported content (invalid UTF-8, malformed JSON,
    non-finite constants, decoder limits, or a payload that does not match the
    request schema) raises ``BusinessError(ErrorCode.DATA_IMPORT_INVALID,
    http_status=422)`` with the fixed generic message ``"Import JSON is
    invalid"``. Raw request content is never echoed into the error.

    Task 10 MUST:

    - bind a FastAPI ``Request`` (not a bound body model), read its stream with
      a bounded helper, and pass those raw bytes to this function;
    - persist those **exact request bytes unchanged** as the import source.

    Task 10 MUST NOT:

    - bind ``DataImportJsonRequest`` directly as the endpoint body, because
      FastAPI's default JSON decoder produces ``float`` values before Pydantic
      can preserve their decimal representation;
    - persist ``model_dump()`` / ``model_dump_json()`` output, because
      reserialization loses decimal precision *and* erases duplicate-key
      evidence from the source bytes.

    Duplicate keys are last-wins at this API layer, which is acceptable for
    immediate schema feedback only. The persisted raw bytes remain the
    validation source of truth, and the worker's strict object-pairs parser
    rejects duplicate row, custom, and root keys deterministically.
    """
    try:
        decoded = json.loads(
            content.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
        return DataImportJsonRequest.model_validate(decoded)
    except (
        UnicodeDecodeError,
        ValueError,
        InvalidOperation,
        RecursionError,
        ValidationError,
    ) as error:
        raise _invalid_json() from error


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class JsonImportRow(BaseModel):
    """One structured JSON data row before dynamic field validation."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    business_date: date
    upstream_customer_id: StrictCustomerId
    downstream_customer_id: StrictCustomerId
    revenue: JsonDecimal
    cost: JsonDecimal
    custom_values: dict[str, JsonCustomValue] = Field(default_factory=dict)


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
    "parse_data_import_json_request",
)
