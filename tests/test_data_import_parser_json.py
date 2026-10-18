"""Shared row validation and strict JSON import parsing tests."""

import copy
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.data_management import (
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportSource,
    DataImportStatus,
    DataTableDefinition,
    DataTableStatus,
)
from app.repositories.data_table import ResolvedDataDefinition
from app.schemas.data_import import (
    DataImportErrorRead,
    DataImportJsonRequest,
    DataImportRead,
    JsonImportRow,
)
from app.services.customer_name import customer_name_lookup_key
from app.services.data_import_parser import (
    CustomerIdentity,
    FieldValue,
    ParsedImportRow,
    RowIssue,
    ValidatedImportRow,
    iter_json_rows,
    json_import_row_count,
    load_customer_name_index,
    validate_raw_row,
)
from tests.customer_helpers import (  # noqa: F401
    make_customer_row,
    make_user,
    session,
)


def _field(
    field_id: int,
    label: str,
    field_type: DataFieldType,
    system_key: DataFieldRole,
    *,
    required: bool,
    sort_order: int,
) -> DataFieldDefinition:
    return DataFieldDefinition(
        id=field_id,
        table_id=1,
        label=label,
        field_type=field_type,
        system_key=system_key,
        required=required,
        sort_order=sort_order,
    )


def make_definition() -> ResolvedDataDefinition:
    fields = (
        _field(
            10,
            "Business date",
            DataFieldType.DATE,
            DataFieldRole.BUSINESS_DATE,
            required=True,
            sort_order=0,
        ),
        _field(
            11,
            "Upstream",
            DataFieldType.CUSTOMER,
            DataFieldRole.UPSTREAM_CUSTOMER,
            required=True,
            sort_order=1,
        ),
        _field(
            12,
            "Downstream",
            DataFieldType.CUSTOMER,
            DataFieldRole.DOWNSTREAM_CUSTOMER,
            required=True,
            sort_order=2,
        ),
        _field(
            13,
            "Revenue",
            DataFieldType.DECIMAL,
            DataFieldRole.REVENUE,
            required=True,
            sort_order=3,
        ),
        _field(
            14,
            "Cost",
            DataFieldType.DECIMAL,
            DataFieldRole.COST,
            required=True,
            sort_order=4,
        ),
        _field(
            20,
            "Text ID",
            DataFieldType.TEXT,
            DataFieldRole.CUSTOM,
            required=True,
            sort_order=10,
        ),
        _field(
            21,
            "Integer",
            DataFieldType.INTEGER,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=11,
        ),
        _field(
            22,
            "Decimal",
            DataFieldType.DECIMAL,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=12,
        ),
        _field(
            23,
            "Date",
            DataFieldType.DATE,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=13,
        ),
        _field(
            24,
            "Boolean",
            DataFieldType.BOOLEAN,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=14,
        ),
        _field(
            25,
            "Channel",
            DataFieldType.ENUM,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=15,
        ),
    )
    options = (
        DataEnumOption(id=101, field_id=25, label="Direct", sort_order=0),
        DataEnumOption(id=102, field_id=25, label="Indirect", sort_order=1),
    )
    table = DataTableDefinition(
        id=1,
        name="Daily detail",
        description=None,
        status=DataTableStatus.DRAFT,
        created_by=1,
    )
    return ResolvedDataDefinition(
        table=table,
        fields=fields,
        fields_by_id={item.id: item for item in fields},
        fields_by_system_key={
            item.system_key: item
            for item in fields
            if item.system_key != DataFieldRole.CUSTOM
        },
        enum_options_by_id={item.id: item for item in options},
        enum_options_by_field_id={25: options},
    )


def make_customer_index() -> dict[str, CustomerIdentity]:
    identities = (
        CustomerIdentity(101, "Upstream Co", ContactStatus.ACTIVE),
        CustomerIdentity(202, "Downstream Co", ContactStatus.ACTIVE),
        CustomerIdentity(303, "Hidden Co", ContactStatus.ACTIVE),
        CustomerIdentity(404, "Deleted Co", ContactStatus.DELETED),
    )
    return {customer_name_lookup_key(item.name): item for item in identities}


def valid_json_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "business_date": "2026-10-18",
        "upstream_customer_id": 101,
        "downstream_customer_id": 202,
        "revenue": "-1942.510000",
        "cost": "1155.140000",
        "custom_values": {
            "20": "004047806",
            "21": "9223372036854775807",
            "22": "1.23456789",
            "23": "2026-10-18",
            "24": False,
            "25": " Direct ",
        },
    }
    row.update(overrides)
    return row


def issues_for(result: ValidatedImportRow | list[RowIssue]) -> list[RowIssue]:
    assert isinstance(result, list)
    return result


def test_json_schema_preserves_decimal_numbers_and_enforces_row_limit() -> None:
    payload = DataImportJsonRequest.model_validate_json(
        json.dumps(
            {
                "rows": [
                    {
                        "business_date": "2026-10-18",
                        "upstream_customer_id": 101,
                        "downstream_customer_id": 202,
                        "revenue": 0.1,
                        "cost": 2,
                        "custom_values": {"22": 1.25},
                    }
                ]
            }
        )
    )

    assert payload.rows[0].revenue == Decimal("0.1")
    assert type(payload.rows[0].revenue) is Decimal
    assert payload.rows[0].cost == Decimal(2)
    assert payload.rows[0].custom_values["22"] == Decimal("1.25")
    assert type(payload.rows[0].custom_values["22"]) is Decimal
    row = JsonImportRow.model_validate(valid_json_row())
    with pytest.raises(ValidationError):
        DataImportJsonRequest(rows=[row] * 100_001)


def test_json_schema_forbids_extra_row_and_root_structure() -> None:
    with pytest.raises(ValidationError) as extra_row:
        JsonImportRow.model_validate(valid_json_row(unexpected="raw-secret"))
    with pytest.raises(ValidationError) as extra_root:
        DataImportJsonRequest.model_validate(
            {"rows": [valid_json_row()], "unexpected": "raw-secret"}
        )
    with pytest.raises(ValidationError):
        JsonImportRow.model_validate(
            valid_json_row(custom_values={"20": {"nested": "raw-secret"}})
        )

    assert "raw-secret" not in str(extra_row.value)
    assert "raw-secret" not in str(extra_root.value)


def test_shared_validator_accepts_and_types_every_supported_field() -> None:
    result = validate_raw_row(
        valid_json_row(),
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.business_date == date(2026, 10, 18)
    assert result.upstream_customer_id == 101
    assert result.downstream_customer_id == 202
    assert result.upstream_customer_name_source == "Upstream Co"
    assert result.downstream_customer_name_source == "Downstream Co"
    assert result.revenue == Decimal("-1942.510000")
    assert result.cost == Decimal("1155.140000")
    assert result.text_values[20] == "004047806"
    assert result.integer_values[21] == 9223372036854775807
    assert result.decimal_values[22] == Decimal("1.23456789")
    assert result.date_values[23] == date(2026, 10, 18)
    assert result.boolean_values[24] is False
    assert result.enum_option_ids[25] == 101
    assert result.custom_values[25] == FieldValue(
        field_id=25,
        enum_option_id=101,
    )


def test_json_decimal_never_uses_binary_float_and_accepts_negative_adjustment() -> None:
    result = validate_raw_row(
        valid_json_row(revenue="-1942.510000", cost="1155.140000"),
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.revenue == Decimal("-1942.510000")
    assert result.cost == Decimal("1155.140000")
    assert type(result.revenue) is Decimal
    assert type(result.cost) is Decimal


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        ("1.123456789", "too_many_decimal_places"),
        ("10000000000000000000000", "decimal_out_of_range"),
    ],
)
def test_decimal_enforces_scale_and_decimal_30_8_range(
    value: str,
    error_code: str,
) -> None:
    row = valid_json_row(revenue=value)

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        (error_code, 13)
    ]


@pytest.mark.parametrize(
    "value",
    ["-9223372036854775808", "9223372036854775807"],
)
def test_integer_accepts_signed_64_bit_boundaries(value: str) -> None:
    row = valid_json_row()
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["21"] = value
    row["custom_values"] = custom_values

    result = validate_raw_row(
        row,
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.integer_values[21] == int(value)


@pytest.mark.parametrize(
    "value",
    [
        "-9223372036854775809",
        "9223372036854775808",
        "9" * 4_301,
        "1.0",
        True,
    ],
)
def test_integer_rejects_out_of_range_and_non_integer_values(value: object) -> None:
    row = valid_json_row()
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["21"] = value
    row["custom_values"] = custom_values

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        ("integer_out_of_range", 21)
        if isinstance(value, str) and value.lstrip("-").isdigit()
        else ("invalid_integer", 21)
    ]


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, "是"])
def test_boolean_requires_json_boolean(value: object) -> None:
    row = valid_json_row()
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["24"] = value
    row["custom_values"] = custom_values

    result = validate_raw_row(
        row,
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    if value is None:
        assert isinstance(result, ValidatedImportRow)
        assert 24 not in result.boolean_values
    else:
        issues = issues_for(result)
        assert [(issue.error_code, issue.field_id) for issue in issues] == [
            ("invalid_boolean", 24)
        ]


@pytest.mark.parametrize("value", ["20261018", "2026-02-30", datetime.now()])
def test_date_requires_iso_date_without_time(value: object) -> None:
    row = valid_json_row(business_date=value)

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        ("invalid_date", 10)
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Direct ", 101),
        ("Indirect", 102),
    ],
)
def test_enum_matches_exact_trimmed_label(value: str, expected: int) -> None:
    row = valid_json_row()
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["25"] = value
    row["custom_values"] = custom_values

    result = validate_raw_row(
        row,
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.enum_option_ids[25] == expected


def test_enum_rejects_case_or_label_mismatch() -> None:
    row = valid_json_row()
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["25"] = " direct "
    row["custom_values"] = custom_values

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        ("invalid_enum", 25)
    ]


def test_required_system_and_custom_fields_are_reported_by_field() -> None:
    row = valid_json_row()
    del row["revenue"]
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    del custom_values["20"]
    row["custom_values"] = custom_values

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [
        (issue.error_code, issue.field_id, issue.column_label) for issue in issues
    ] == [
        ("required", 13, "Revenue"),
        ("required", 20, "Text ID"),
    ]


def test_unknown_and_system_fields_are_rejected_in_scope() -> None:
    row = valid_json_row(extra_system="secret")
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["999"] = "secret"
    custom_values["business_date"] = "2026-10-18"
    custom_values["10"] = "2026-10-18"
    row["custom_values"] = custom_values

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        ("unknown_field", None),
        ("unknown_custom_field", None),
        ("system_field_in_custom_values", 10),
        ("system_field_in_custom_values", 10),
    ]


def test_text_accepts_4000_unicode_characters_and_rejects_4001() -> None:
    accepted = valid_json_row()
    custom_values = copy.deepcopy(accepted["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["20"] = "界" * 4_000
    accepted["custom_values"] = custom_values

    result = validate_raw_row(
        accepted,
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.text_values[20] == "界" * 4_000

    rejected = copy.deepcopy(accepted)
    rejected_values = rejected["custom_values"]
    assert isinstance(rejected_values, dict)
    rejected_values["20"] = "界" * 4_001
    issues = issues_for(
        validate_raw_row(
            rejected,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )
    assert [(issue.error_code, issue.field_id) for issue in issues] == [
        ("text_too_long", 20)
    ]


def test_customer_ids_must_be_active_visible_and_authorized() -> None:
    definition = make_definition()
    customer_index = make_customer_index()
    cases = (
        (valid_json_row(upstream_customer_id=404), None, ("invalid_customer", 11)),
        (
            valid_json_row(upstream_customer_id=303),
            {101, 202},
            ("customer_out_of_scope", 11),
        ),
        (valid_json_row(), {101}, ("customer_out_of_scope", 12)),
        (valid_json_row(upstream_customer_id=999), None, ("invalid_customer", 11)),
    )

    for row, allowed, expected in cases:
        issues = issues_for(
            validate_raw_row(
                row,
                definition,
                customer_index,
                allowed_customer_ids=allowed,
            )
        )
        assert [(issue.error_code, issue.field_id) for issue in issues] == [expected]


def test_customer_scope_allows_both_visible_customer_ids() -> None:
    result = validate_raw_row(
        valid_json_row(),
        make_definition(),
        make_customer_index(),
        allowed_customer_ids={101, 202},
    )

    assert isinstance(result, ValidatedImportRow)
    assert result.upstream_customer_id == 101
    assert result.downstream_customer_id == 202


def test_issue_messages_do_not_include_raw_values_or_customer_names() -> None:
    row = valid_json_row(
        upstream_customer_id=999,
        revenue="RAW-REVENUE-SECRET",
        unexpected="RAW-EXTRA-SECRET",
    )
    custom_values = copy.deepcopy(row["custom_values"])
    assert isinstance(custom_values, dict)
    custom_values["20"] = "RAW-TEXT-SECRET" * 300
    row["custom_values"] = custom_values

    issues = issues_for(
        validate_raw_row(
            row,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )
    rendered = " ".join(
        f"{issue.error_code} {issue.column_label or ''} {issue.message}"
        for issue in issues
    )

    for secret in (
        "RAW-REVENUE-SECRET",
        "RAW-EXTRA-SECRET",
        "RAW-TEXT-SECRET",
        "Upstream Co",
        "Deleted Co",
    ):
        assert secret not in rendered


def test_json_iterator_decodes_decimal_without_float(tmp_path: Path) -> None:
    row = valid_json_row()
    row["revenue"] = "__REVENUE_NUMBER__"
    path = tmp_path / "rows.json"
    path.write_text(json.dumps({"rows": [row]}).replace('"__REVENUE_NUMBER__"', "0.1"))

    parsed = list(
        iter_json_rows(
            path,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert len(parsed) == 1
    assert isinstance(parsed[0], ParsedImportRow)
    assert parsed[0].row_number == 1
    assert parsed[0].issues == ()
    assert parsed[0].row is not None
    assert parsed[0].row.revenue == Decimal("0.1")
    assert type(parsed[0].row.revenue) is Decimal


def test_json_iterator_returns_row_scoped_issues(tmp_path: Path) -> None:
    path = tmp_path / "rows.json"
    path.write_text(json.dumps({"rows": [valid_json_row(revenue="secret")]}))

    parsed = list(
        iter_json_rows(
            path,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert len(parsed) == 1
    assert parsed[0].row_number == 1
    assert parsed[0].row is None
    assert [(issue.error_code, issue.field_id) for issue in parsed[0].issues] == [
        ("invalid_decimal", 13)
    ]
    assert "secret" not in parsed[0].issues[0].message


def test_json_iterator_rejects_duplicate_row_and_custom_keys(tmp_path: Path) -> None:
    row = valid_json_row()
    row["revenue"] = "__REVENUE__"
    body = json.dumps({"rows": [row]}).replace(
        '"business_date": "2026-10-18"',
        '"business_date": "2026-10-18", "business_date": "2026-10-19"',
    )
    body = body.replace(
        '"20": "004047806"',
        '"20": "004047806", "20": "RAW-DUPLICATE-SECRET"',
    )
    body = body.replace('"__REVENUE__"', '"1.0"')
    path = tmp_path / "duplicates.json"
    path.write_text(body)

    parsed = list(
        iter_json_rows(
            path,
            make_definition(),
            make_customer_index(),
            allowed_customer_ids=None,
        )
    )

    assert len(parsed) == 1
    assert parsed[0].row is None
    assert [(issue.error_code, issue.field_id) for issue in parsed[0].issues] == [
        ("duplicate_key", 10),
        ("duplicate_key", 20),
    ]
    assert all(
        "RAW-DUPLICATE-SECRET" not in issue.message for issue in parsed[0].issues
    )


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"rows": {}}',
        '{"rows": [], "extra": true}',
        '{"rows": [null]}',
        "{",
    ],
)
def test_json_root_must_be_one_object_with_rows_list(
    content: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(content)

    if content == '{"rows": [null]}':
        parsed = list(
            iter_json_rows(
                path,
                make_definition(),
                make_customer_index(),
                allowed_customer_ids=None,
            )
        )
        assert parsed[0].row is None
        assert parsed[0].issues[0].error_code == "invalid_row"
    else:
        with pytest.raises(BusinessError) as error:
            list(
                iter_json_rows(
                    path,
                    make_definition(),
                    make_customer_index(),
                    allowed_customer_ids=None,
                )
            )
        assert error.value.code == ErrorCode.DATA_IMPORT_INVALID


def test_json_row_count_enforces_maximum(tmp_path: Path) -> None:
    one_row = tmp_path / "one.json"
    one_row.write_text(json.dumps({"rows": [valid_json_row()]}))
    assert json_import_row_count(one_row) == 1

    too_many = tmp_path / "too-many.json"
    too_many.write_text('{"rows":[' + ",".join("{}" for _ in range(100_001)) + "]}")
    with pytest.raises(BusinessError) as error:
        json_import_row_count(too_many)
    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID


async def test_load_customer_name_index_is_active_only_and_optional_visible_filtering(
    session: AsyncSession,  # noqa: F811
) -> None:
    owner = await make_user(session)
    first = await make_customer_row(
        session,
        owner=owner,
        name="  Café Alpha  ",
    )
    second = await make_customer_row(session, owner=owner, name="Beta Co")
    deleted = await make_customer_row(session, owner=owner, name="Deleted Co")
    deleted.status = ContactStatus.DELETED
    deleted.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()

    all_active = await load_customer_name_index(session)
    assert set(all_active) == {
        customer_name_lookup_key("Café Alpha"),
        customer_name_lookup_key("Beta Co"),
    }
    assert all_active[customer_name_lookup_key("CAFE ALPHA")].id == first.id
    assert all_active[customer_name_lookup_key("BETA CO")].id == second.id
    assert all_active[customer_name_lookup_key("CAFE ALPHA")].name == "Café Alpha"
    assert (
        all_active[customer_name_lookup_key("CAFE ALPHA")].status
        == ContactStatus.ACTIVE
    )
    assert deleted.id not in {item.id for item in all_active.values()}

    visible = await load_customer_name_index(session, {first.id})
    assert [item.id for item in visible.values()] == [first.id]
    assert await load_customer_name_index(session, {deleted.id}) == {}
    assert await load_customer_name_index(session, set()) == {}


def test_import_read_schemas_serialize_safe_public_fields() -> None:
    queued_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    read = DataImportRead(
        id=1,
        table_id=2,
        source=DataImportSource.API,
        status=DataImportStatus.QUEUED,
        original_filename=None,
        size_bytes=2,
        requested_by=3,
        total_rows=1,
        valid_rows=0,
        invalid_rows=0,
        date_from=None,
        date_to=None,
        error_count=0,
        progress_percent=0,
        failure_code=None,
        failure_message=None,
        attempt_count=0,
        queued_at=queued_at,
        started_at=None,
        finished_at=None,
        created_at=queued_at,
        updated_at=queued_at,
    )
    error = DataImportErrorRead(
        id=4,
        import_id=1,
        sheet_name=None,
        row_number=1,
        column_label="Revenue",
        field_id=13,
        error_code="invalid_decimal",
        message="Value must be a decimal",
        created_at=queued_at,
    )

    serialized = read.model_dump(mode="json")
    assert serialized["queued_at"] == "2026-10-18T12:00:00+00:00"
    assert "storage_path" not in serialized
    assert error.model_dump(mode="json")["error_code"] == "invalid_decimal"


def test_public_dataclass_contract_is_mapping_friendly() -> None:
    result = validate_raw_row(
        valid_json_row(),
        make_definition(),
        make_customer_index(),
        allowed_customer_ids=None,
    )
    assert isinstance(result, ValidatedImportRow)

    values: Mapping[int, FieldValue] = result.custom_values
    assert values[20].text_value == "004047806"
    assert isinstance(values[20], FieldValue)
