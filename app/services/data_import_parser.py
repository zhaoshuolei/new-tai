"""Shared typed row validation and strict JSON import parsing.

The conversion layer intentionally accepts domain values rather than depending on
an Excel or JSON transport. JSON-specific parsing adds strict ISO dates, JSON
booleans, duplicate-key detection, and root-shape enforcement.
"""

import json
import re
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.data_management import (
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
)
from app.repositories.data_table import ResolvedDataDefinition
from app.services.customer_name import customer_name_lookup_key

_MAX_ROWS = 100_000
_MAX_TEXT_LENGTH = 4_000
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_DECIMAL_INTEGER_DIGITS = 22
_MAX_DECIMAL_PLACES = 8
_ISO_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_INTEGER_PATTERN = re.compile(r"^-?[0-9]+$")
_DECIMAL_PATTERN = re.compile(
    r"^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
_FIELD_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,18}$")

_FIXED_JSON_KEYS: dict[DataFieldRole, str] = {
    DataFieldRole.BUSINESS_DATE: "business_date",
    DataFieldRole.UPSTREAM_CUSTOMER: "upstream_customer_id",
    DataFieldRole.DOWNSTREAM_CUSTOMER: "downstream_customer_id",
    DataFieldRole.REVENUE: "revenue",
    DataFieldRole.COST: "cost",
}
_FIXED_JSON_KEY_SET = frozenset(_FIXED_JSON_KEYS.values())


@dataclass(frozen=True)
class CustomerIdentity:
    """The small active-customer projection used during import validation."""

    id: int
    name: str
    status: ContactStatus


@dataclass(frozen=True)
class FieldValue:
    """One custom field value mapped to the typed value-table columns."""

    field_id: int
    text_value: str | None = None
    integer_value: int | None = None
    decimal_value: Decimal | None = None
    date_value: date | None = None
    boolean_value: bool | None = None
    enum_option_id: int | None = None


@dataclass(frozen=True)
class RowIssue:
    """A safe, row-scoped validation error with optional field location."""

    error_code: str
    message: str
    field_id: int | None = None
    column_label: str | None = None
    sheet_name: str | None = None
    row_number: int | None = None


@dataclass(frozen=True)
class ValidatedImportRow:
    """A fully typed row ready for snapshot persistence."""

    business_date: date
    upstream_customer_id: int
    downstream_customer_id: int
    upstream_customer_name_source: str
    downstream_customer_name_source: str
    revenue: Decimal
    cost: Decimal
    custom_values: Mapping[int, FieldValue]

    @property
    def field_values(self) -> tuple[FieldValue, ...]:
        return tuple(self.custom_values.values())

    @property
    def text_values(self) -> dict[int, str]:
        return {
            field_id: value.text_value
            for field_id, value in self.custom_values.items()
            if value.text_value is not None
        }

    @property
    def integer_values(self) -> dict[int, int]:
        return {
            field_id: value.integer_value
            for field_id, value in self.custom_values.items()
            if value.integer_value is not None
        }

    @property
    def decimal_values(self) -> dict[int, Decimal]:
        return {
            field_id: value.decimal_value
            for field_id, value in self.custom_values.items()
            if value.decimal_value is not None
        }

    @property
    def date_values(self) -> dict[int, date]:
        return {
            field_id: value.date_value
            for field_id, value in self.custom_values.items()
            if value.date_value is not None
        }

    @property
    def boolean_values(self) -> dict[int, bool]:
        return {
            field_id: value.boolean_value
            for field_id, value in self.custom_values.items()
            if value.boolean_value is not None
        }

    @property
    def enum_option_ids(self) -> dict[int, int]:
        return {
            field_id: value.enum_option_id
            for field_id, value in self.custom_values.items()
            if value.enum_option_id is not None
        }


@dataclass(frozen=True)
class ParsedImportRow:
    """One source row, either validated or carrying all row-scoped issues."""

    row_number: int
    row: ValidatedImportRow | None = None
    issues: tuple[RowIssue, ...] = ()


class _JsonObject(MutableMapping[str, object]):
    """A decoded object that retains duplicate-key evidence."""

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        values: dict[str, object] = {}
        duplicate_keys: list[str] = []
        for key, value in pairs:
            if key in values:
                duplicate_keys.append(key)
            values[key] = value
        self._values = values
        self.duplicate_keys = tuple(duplicate_keys)

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _ConversionError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _invalid_json() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_INVALID,
        "Import JSON is invalid",
        http_status=422,
    )


def _reject_json_constant(constant: str) -> object:
    raise ValueError(f"Non-standard JSON constant: {constant}")


def _load_json(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(
                source,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_JsonObject,
            )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
        RecursionError,
        ValueError,
        InvalidOperation,
    ) as error:
        raise _invalid_json() from error
    return cast(object, value)


def _load_json_rows(path: Path) -> list[object]:
    document = _load_json(path)
    if not isinstance(document, Mapping):
        raise _invalid_json()
    if isinstance(document, _JsonObject) and document.duplicate_keys:
        raise _invalid_json()
    if set(document) != {"rows"}:
        raise _invalid_json()
    rows = document["rows"]
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise _invalid_json()
    return rows


def json_import_row_count(path: Path) -> int:
    """Return the row count after enforcing the complete JSON envelope."""
    return len(_load_json_rows(path))


def iter_json_rows(
    path: Path,
    definition: ResolvedDataDefinition,
    customer_index: Mapping[str, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
) -> Iterator[ParsedImportRow]:
    """Yield one validated or issue-carrying result per JSON row."""
    customers_by_id = _customer_index_by_id(customer_index)
    for row_number, raw in enumerate(_load_json_rows(path), start=1):
        if not isinstance(raw, Mapping):
            yield ParsedImportRow(
                row_number=row_number,
                issues=(RowIssue("invalid_row", "Row must be a JSON object"),),
            )
            continue

        duplicate_issues = _duplicate_issue_rows(raw, definition, row_number)
        validation = _validate_raw_row(
            raw,
            definition,
            customers_by_id,
            allowed_customer_ids,
        )
        if isinstance(validation, ValidatedImportRow) and not duplicate_issues:
            yield ParsedImportRow(row_number=row_number, row=validation)
            continue

        validation_issues = validation if isinstance(validation, list) else []
        issues = [
            RowIssue(
                error_code=issue.error_code,
                message=issue.message,
                field_id=issue.field_id,
                column_label=issue.column_label,
                sheet_name=issue.sheet_name,
                row_number=row_number,
            )
            for issue in [*duplicate_issues, *validation_issues]
        ]
        yield ParsedImportRow(row_number=row_number, issues=tuple(issues))


async def load_customer_name_index(
    session: AsyncSession,
    visible_customer_ids: set[int] | None = None,
) -> dict[str, CustomerIdentity]:
    """Load active customer names, optionally restricted to visible IDs."""
    if visible_customer_ids is not None and not visible_customer_ids:
        return {}

    statement = select(Customer).where(
        Customer.status == ContactStatus.ACTIVE,
        Customer.deleted_at.is_(None),
    )
    if visible_customer_ids is not None:
        statement = statement.where(Customer.id.in_(visible_customer_ids))
    customers = list((await session.scalars(statement)).all())
    return {
        customer_name_lookup_key(customer.name): CustomerIdentity(
            id=customer.id,
            name=customer.name.strip(),
            status=customer.status,
        )
        for customer in customers
    }


def _customer_index_by_id(
    customer_index: Mapping[str, CustomerIdentity],
) -> dict[int, CustomerIdentity]:
    return {identity.id: identity for identity in customer_index.values()}


def validate_raw_row(
    raw: Mapping[str, object],
    definition: ResolvedDataDefinition,
    customer_index: Mapping[str, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
) -> ValidatedImportRow | list[RowIssue]:
    """Validate one transport-neutral row against a resolved definition."""
    return _validate_raw_row(
        raw,
        definition,
        _customer_index_by_id(customer_index),
        allowed_customer_ids,
    )


def _validate_raw_row(
    raw: Mapping[str, object],
    definition: ResolvedDataDefinition,
    customers_by_id: Mapping[int, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
) -> ValidatedImportRow | list[RowIssue]:
    """Validate a row against an already-built customer-ID index."""
    issues: list[RowIssue] = []
    system_values: dict[DataFieldRole, object] = {}

    for role, raw_key in _FIXED_JSON_KEYS.items():
        field = _required_system_field(definition, role)
        if raw.get(raw_key) is None:
            issues.append(_required_issue(field))
            continue
        value = raw[raw_key]
        try:
            if role == DataFieldRole.BUSINESS_DATE:
                system_values[role] = _coerce_date(value)
            elif role in (
                DataFieldRole.UPSTREAM_CUSTOMER,
                DataFieldRole.DOWNSTREAM_CUSTOMER,
            ):
                customer_id = _coerce_customer_id(value)
                system_values[role] = _resolve_customer(
                    customer_id,
                    customers_by_id,
                    allowed_customer_ids,
                )
            else:
                system_values[role] = _coerce_decimal(value)
        except _ConversionError as error:
            issues.append(_conversion_issue(field, error))

    allowed_keys = _FIXED_JSON_KEY_SET | {"custom_values"}
    for key in raw:
        if key not in allowed_keys:
            issues.append(
                RowIssue(
                    "unknown_field",
                    "Row contains an unknown field",
                )
            )

    custom_values_raw = raw.get("custom_values", {})
    if custom_values_raw is None:
        custom_values_raw = {}
    if not isinstance(custom_values_raw, Mapping):
        issues.append(
            RowIssue(
                "invalid_custom_values",
                "custom_values must be an object keyed by field ID",
            )
        )
        custom_values_raw = {}

    custom_values = _validate_custom_values(
        custom_values_raw,
        definition,
        issues,
    )
    if issues:
        return issues

    upstream = cast(
        CustomerIdentity,
        system_values[DataFieldRole.UPSTREAM_CUSTOMER],
    )
    downstream = cast(
        CustomerIdentity,
        system_values[DataFieldRole.DOWNSTREAM_CUSTOMER],
    )
    return ValidatedImportRow(
        business_date=cast(date, system_values[DataFieldRole.BUSINESS_DATE]),
        upstream_customer_id=upstream.id,
        downstream_customer_id=downstream.id,
        upstream_customer_name_source=upstream.name,
        downstream_customer_name_source=downstream.name,
        revenue=cast(Decimal, system_values[DataFieldRole.REVENUE]),
        cost=cast(Decimal, system_values[DataFieldRole.COST]),
        custom_values=custom_values,
    )


def _required_system_field(
    definition: ResolvedDataDefinition,
    role: DataFieldRole,
) -> DataFieldDefinition:
    field = definition.field_for(role)
    if field is None:
        raise BusinessError(
            ErrorCode.DATA_TABLE_INVALID,
            "Resolved data definition is invalid",
            http_status=422,
        )
    return field


def _validate_custom_values(
    raw_custom_values: Mapping[str, object],
    definition: ResolvedDataDefinition,
    issues: list[RowIssue],
) -> dict[int, FieldValue]:
    values_by_field_id: dict[int, object] = {}
    for raw_field_id, raw_value in raw_custom_values.items():
        field = _custom_field_for_key(raw_field_id, definition)
        if field is None:
            system_field = _system_field_for_key(raw_field_id, definition)
            if system_field is not None:
                issues.append(
                    RowIssue(
                        "system_field_in_custom_values",
                        "System fields must be supplied as top-level row keys",
                        field_id=system_field.id,
                        column_label=system_field.label,
                    )
                )
            else:
                issues.append(
                    RowIssue(
                        "unknown_custom_field",
                        "custom_values contains an unknown field ID",
                    )
                )
            continue

        if field.system_key != DataFieldRole.CUSTOM:
            issues.append(
                RowIssue(
                    "system_field_in_custom_values",
                    "System fields must be supplied as top-level row keys",
                    field_id=field.id,
                    column_label=field.label,
                )
            )
            continue
        if field.id in values_by_field_id:
            issues.append(
                RowIssue(
                    "duplicate_key",
                    "custom_values contains the same field ID more than once",
                    field_id=field.id,
                    column_label=field.label,
                )
            )
            continue
        values_by_field_id[field.id] = raw_value

    converted: dict[int, FieldValue] = {}
    for field in definition.custom_fields:
        if field.id not in values_by_field_id:
            if field.required:
                issues.append(_required_issue(field))
            continue
        raw_value = values_by_field_id[field.id]
        if raw_value is None:
            if field.required:
                issues.append(_required_issue(field))
            continue
        try:
            converted[field.id] = _coerce_custom_value(field, raw_value, definition)
        except _ConversionError as error:
            issues.append(_conversion_issue(field, error))
    return converted


def _custom_field_for_key(
    raw_field_id: object,
    definition: ResolvedDataDefinition,
) -> DataFieldDefinition | None:
    if not isinstance(raw_field_id, str) or not _FIELD_ID_PATTERN.fullmatch(
        raw_field_id
    ):
        return None
    field_id = int(raw_field_id)
    if field_id > _INT64_MAX:
        return None
    return definition.fields_by_id.get(field_id)


def _system_field_for_key(
    raw_key: object,
    definition: ResolvedDataDefinition,
) -> DataFieldDefinition | None:
    if not isinstance(raw_key, str):
        return None
    role_by_json_key = {value: role for role, value in _FIXED_JSON_KEYS.items()}
    folded_key = raw_key.casefold()
    for json_key, role in role_by_json_key.items():
        if folded_key == json_key.casefold():
            return definition.field_for(role)
    try:
        role = DataFieldRole(raw_key.upper())
    except ValueError:
        return None
    if role == DataFieldRole.CUSTOM:
        return None
    return definition.field_for(role)


def _coerce_custom_value(
    field: DataFieldDefinition,
    raw_value: object,
    definition: ResolvedDataDefinition,
) -> FieldValue:
    if field.field_type == DataFieldType.TEXT:
        if not isinstance(raw_value, str):
            raise _ConversionError("invalid_text", "Value must be text")
        if len(raw_value) > _MAX_TEXT_LENGTH:
            raise _ConversionError(
                "text_too_long",
                f"Text must not exceed {_MAX_TEXT_LENGTH} characters",
            )
        return FieldValue(field_id=field.id, text_value=raw_value)
    if field.field_type == DataFieldType.INTEGER:
        return FieldValue(
            field_id=field.id,
            integer_value=_coerce_integer(raw_value),
        )
    if field.field_type == DataFieldType.DECIMAL:
        return FieldValue(
            field_id=field.id,
            decimal_value=_coerce_decimal(raw_value),
        )
    if field.field_type == DataFieldType.DATE:
        return FieldValue(
            field_id=field.id,
            date_value=_coerce_date(raw_value),
        )
    if field.field_type == DataFieldType.BOOLEAN:
        return FieldValue(
            field_id=field.id,
            boolean_value=_coerce_boolean(raw_value),
        )
    if field.field_type == DataFieldType.ENUM:
        return FieldValue(
            field_id=field.id,
            enum_option_id=_coerce_enum(raw_value, field, definition),
        )
    raise _ConversionError(
        "unsupported_field_type",
        f"Custom field type {field.field_type.value} is not supported",
    )


def _coerce_integer(value: object) -> int:
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if _INTEGER_PATTERN.fullmatch(candidate):
            digits = candidate.removeprefix("-")
            if len(digits) > 19:
                raise _ConversionError(
                    "integer_out_of_range",
                    "Integer is outside the signed 64-bit range",
                )
            parsed = int(candidate)
    if parsed is None:
        raise _ConversionError("invalid_integer", "Value must be an integer")
    if parsed < _INT64_MIN or parsed > _INT64_MAX:
        raise _ConversionError(
            "integer_out_of_range",
            "Integer is outside the signed 64-bit range",
        )
    return parsed


def _coerce_decimal(value: object) -> Decimal:
    parsed: Decimal
    if isinstance(value, bool) or isinstance(value, float):
        raise _ConversionError("invalid_decimal", "Value must be a decimal")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        candidate = value.strip()
        if not _DECIMAL_PATTERN.fullmatch(candidate):
            raise _ConversionError("invalid_decimal", "Value must be a decimal")
        try:
            parsed = Decimal(candidate)
        except InvalidOperation as error:
            raise _ConversionError(
                "invalid_decimal",
                "Value must be a decimal",
            ) from error
    else:
        raise _ConversionError("invalid_decimal", "Value must be a decimal")

    if not parsed.is_finite():
        raise _ConversionError("invalid_decimal", "Value must be a finite decimal")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int):
        raise _ConversionError("invalid_decimal", "Value must be a finite decimal")
    if exponent < -_MAX_DECIMAL_PLACES:
        raise _ConversionError(
            "too_many_decimal_places",
            f"Decimal must not exceed {_MAX_DECIMAL_PLACES} decimal places",
        )
    if parsed != 0 and parsed.adjusted() >= _MAX_DECIMAL_INTEGER_DIGITS:
        raise _ConversionError(
            "decimal_out_of_range",
            "Decimal is outside the supported DECIMAL(30,8) range",
        )
    return parsed


def _coerce_date(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, (date, str)):
        raise _ConversionError("invalid_date", "Value must be an ISO date")
    if isinstance(value, date):
        return value
    candidate = value.strip()
    if not _ISO_DATE_PATTERN.fullmatch(candidate):
        raise _ConversionError("invalid_date", "Value must be an ISO date")
    try:
        return date.fromisoformat(candidate)
    except ValueError as error:
        raise _ConversionError(
            "invalid_date",
            "Value must be a valid ISO date",
        ) from error


def _coerce_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _ConversionError("invalid_boolean", "Value must be a boolean")
    return value


def _coerce_enum(
    value: object,
    field: DataFieldDefinition,
    definition: ResolvedDataDefinition,
) -> int:
    if not isinstance(value, str):
        raise _ConversionError("invalid_enum", "Value must be an enum label")
    label = value.strip()
    for option in definition.options_for(field.id):
        if option.label == label:
            return option.id
    raise _ConversionError("invalid_enum", "Value is not an allowed enum label")


def _coerce_customer_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _ConversionError("invalid_customer", "Customer ID is invalid")
    return value


def _resolve_customer(
    customer_id: int,
    customers_by_id: Mapping[int, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
) -> CustomerIdentity:
    identity = customers_by_id.get(customer_id)
    if identity is None or identity.status != ContactStatus.ACTIVE:
        raise _ConversionError("invalid_customer", "Customer is unavailable")
    if allowed_customer_ids is not None and customer_id not in allowed_customer_ids:
        raise _ConversionError(
            "customer_out_of_scope",
            "Customer is outside your data scope",
        )
    return identity


def _required_issue(field: DataFieldDefinition) -> RowIssue:
    return RowIssue(
        "required",
        f"Field {field.label} is required",
        field_id=field.id,
        column_label=field.label,
    )


def _conversion_issue(
    field: DataFieldDefinition,
    error: _ConversionError,
) -> RowIssue:
    return RowIssue(
        error.error_code,
        f"Field {field.label}: {error.message}",
        field_id=field.id,
        column_label=field.label,
    )


def _duplicate_issue_rows(
    raw: Mapping[str, object],
    definition: ResolvedDataDefinition,
    row_number: int,
) -> tuple[RowIssue, ...]:
    issues = _duplicate_object_issues(raw, definition)
    custom_values = raw.get("custom_values")
    if isinstance(custom_values, Mapping):
        issues.extend(_duplicate_object_issues(custom_values, definition))
    return tuple(
        RowIssue(
            error_code=issue.error_code,
            message=issue.message,
            field_id=issue.field_id,
            column_label=issue.column_label,
            sheet_name=issue.sheet_name,
            row_number=row_number,
        )
        for issue in issues
    )


def _duplicate_object_issues(
    raw: Mapping[str, object],
    definition: ResolvedDataDefinition,
) -> list[RowIssue]:
    if not isinstance(raw, _JsonObject):
        return []
    issues: list[RowIssue] = []
    for key in raw.duplicate_keys:
        field = _field_for_duplicate_key(key, definition)
        issues.append(
            RowIssue(
                "duplicate_key",
                "Object contains the same key more than once",
                field_id=field.id if field is not None else None,
                column_label=field.label if field is not None else None,
            )
        )
    return issues


def _field_for_duplicate_key(
    key: str,
    definition: ResolvedDataDefinition,
) -> DataFieldDefinition | None:
    role_by_json_key = {value: role for role, value in _FIXED_JSON_KEYS.items()}
    if key in role_by_json_key:
        return definition.field_for(role_by_json_key[key])
    return _custom_field_for_key(key, definition)


__all__: tuple[str, ...] = (
    "CustomerIdentity",
    "FieldValue",
    "ParsedImportRow",
    "RowIssue",
    "ValidatedImportRow",
    "iter_json_rows",
    "json_import_row_count",
    "load_customer_name_index",
    "validate_raw_row",
)
