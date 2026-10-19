"""Excel workbook parsing and template generation tests."""

import io
import logging
import warnings
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence, ValuesView
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.data_management import (
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataTableDefinition,
    DataTableStatus,
)
from app.repositories.data_table import ResolvedDataDefinition
from app.services.customer_name import customer_name_lookup_key
from app.services.data_import_parser import (
    CustomerIdentity,
    ParsedImportRow,
    ValidatedImportRow,
    build_excel_template,
    iter_excel_rows,
)

EXCEL_INVALID = ErrorCode.DATA_IMPORT_INVALID


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


def make_unicode_definition() -> ResolvedDataDefinition:
    fields = (
        _field(
            30,
            "业务日期",
            DataFieldType.DATE,
            DataFieldRole.BUSINESS_DATE,
            required=True,
            sort_order=0,
        ),
        _field(
            31,
            "上游客户",
            DataFieldType.CUSTOMER,
            DataFieldRole.UPSTREAM_CUSTOMER,
            required=True,
            sort_order=1,
        ),
        _field(
            32,
            "下游客户",
            DataFieldType.CUSTOMER,
            DataFieldRole.DOWNSTREAM_CUSTOMER,
            required=True,
            sort_order=2,
        ),
        _field(
            33,
            "收入",
            DataFieldType.DECIMAL,
            DataFieldRole.REVENUE,
            required=True,
            sort_order=3,
        ),
        _field(
            34,
            "成本",
            DataFieldType.DECIMAL,
            DataFieldRole.COST,
            required=True,
            sort_order=4,
        ),
        _field(
            35,
            "备注",
            DataFieldType.TEXT,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=10,
        ),
    )
    table = DataTableDefinition(
        id=2,
        name="中文表",
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
        enum_options_by_id={},
        enum_options_by_field_id={},
    )


def make_customer_index() -> dict[str, CustomerIdentity]:
    identities = (
        CustomerIdentity(101, "Upstream Co", ContactStatus.ACTIVE),
        CustomerIdentity(202, "Downstream Co", ContactStatus.ACTIVE),
        CustomerIdentity(303, "Café Alpha", ContactStatus.ACTIVE),
        CustomerIdentity(404, "Deleted Co", ContactStatus.DELETED),
        CustomerIdentity(505, "Hidden Co", ContactStatus.ACTIVE),
    )
    return {customer_name_lookup_key(item.name): item for item in identities}


class CountingCustomerIndex(dict[str, CustomerIdentity]):
    def __init__(self, values: Mapping[str, CustomerIdentity]) -> None:
        super().__init__(values)
        self.values_calls = 0

    def values(self) -> ValuesView[CustomerIdentity]:
        self.values_calls += 1
        return super().values()


DEFINITION = make_definition()


def make_wide_definition() -> ResolvedDataDefinition:
    """Return the approved five-system/100-custom/105-column envelope."""
    base = make_definition()
    custom_fields = tuple(
        _field(
            1_000 + index,
            f"Wide {index}",
            DataFieldType.TEXT,
            DataFieldRole.CUSTOM,
            required=False,
            sort_order=100 + index,
        )
        for index in range(1, 95)
    )
    fields = (*base.fields, *custom_fields)
    return ResolvedDataDefinition(
        table=base.table,
        fields=fields,
        fields_by_id={item.id: item for item in fields},
        fields_by_system_key=base.fields_by_system_key,
        enum_options_by_id=base.enum_options_by_id,
        enum_options_by_field_id=base.enum_options_by_field_id,
    )


def excel_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Business date": datetime(2026, 10, 18),
        "Upstream": "Upstream Co",
        "Downstream": "Downstream Co",
        "Revenue": 1942.51,
        "Cost": 1155.14,
        "Text ID": "004047806",
        "Integer": 42,
        "Decimal": 1.25,
        "Date": datetime(2026, 10, 18),
        "Boolean": True,
        "Channel": "Direct",
    }
    row.update(overrides)
    return row


def write_rows_workbook(
    tmp_path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    headers: Sequence[object] | None = None,
    title: str = "Data",
    filename: str = "import.xlsx",
    extra_sheet: bool = False,
    chartsheet: bool = False,
    definition: ResolvedDataDefinition = DEFINITION,
) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    labels = list(definition.labels) if headers is None else list(headers)
    worksheet.append(labels)
    for row in rows:
        worksheet.append([row.get(label) if label in row else None for label in labels])
    if extra_sheet:
        workbook.create_sheet("Extra")
    if chartsheet:
        workbook.create_chartsheet()
    path = tmp_path / filename
    workbook.save(path)
    workbook.close()
    return path


def write_raw_workbook(
    tmp_path: Path,
    cells: Sequence[Sequence[object | None]],
    *,
    title: str = "Data",
    filename: str = "import.xlsx",
) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row_index, values in enumerate(cells, start=1):
        for column_index, value in enumerate(values, start=1):
            if value is not None:
                worksheet.cell(row=row_index, column=column_index, value=value)
    path = tmp_path / filename
    workbook.save(path)
    workbook.close()
    return path


def parse(
    path: Path,
    *,
    definition: ResolvedDataDefinition = DEFINITION,
    customer_index: Mapping[str, CustomerIdentity] | None = None,
    allowed_customer_ids: set[int] | None = None,
) -> list[ParsedImportRow]:
    return list(
        iter_excel_rows(
            path,
            definition,
            make_customer_index() if customer_index is None else customer_index,
            allowed_customer_ids,
        )
    )


def single_valid(path: Path) -> ValidatedImportRow:
    results = parse(path)
    assert len(results) == 1
    assert results[0].issues == ()
    assert results[0].row is not None
    return results[0].row


def issue_tuples(result: ParsedImportRow) -> list[tuple[str, int | None, str | None]]:
    return [
        (issue.error_code, issue.field_id, issue.column_label)
        for issue in result.issues
    ]


def assert_invalid_excel(path: Path, message: str | None = None) -> BusinessError:
    with pytest.raises(BusinessError) as error:
        parse(path)
    assert error.value.code == EXCEL_INVALID
    assert error.value.code == 9006
    assert error.value.http_status == 422
    if message is not None:
        assert error.value.message == message
    return error.value


class MutableZip:
    """Rewrite entries of an XLSX archive without duplicate ZIP members."""

    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = entries

    def read(self, name: str) -> bytes:
        return self.entries[name]

    def writestr(self, name: str, content: bytes) -> None:
        self.entries[name] = content


def rewrite_zip(path: Path, mutate: Callable[[MutableZip], None]) -> Path:
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    archive = MutableZip(entries)
    mutate(archive)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in archive.entries.items():
            target.writestr(name, content)
    path.write_bytes(buffer.getvalue())
    return path


def set_workbook_sheet_state(path: Path, state: str) -> Path:
    def mutate(target: MutableZip) -> None:
        workbook_xml = target.read("xl/workbook.xml")
        assert b"state=" not in workbook_xml or b'state="visible"' in workbook_xml
        if b'state="visible"' in workbook_xml:
            rewritten = workbook_xml.replace(
                b'state="visible"',
                f'state="{state}"'.encode(),
                1,
            )
        else:
            rewritten = workbook_xml.replace(
                b"<sheet ",
                f'<sheet state="{state}" '.encode(),
                1,
            )
        target.writestr("xl/workbook.xml", rewritten)

    return rewrite_zip(path, mutate)


# ---------------------------------------------------------------------------
# Supported value matrix
# ---------------------------------------------------------------------------


def test_excel_reads_every_supported_field_type(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    row = single_valid(path)

    assert row.business_date == date(2026, 10, 18)
    assert row.upstream_customer_id == 101
    assert row.downstream_customer_id == 202
    assert row.upstream_customer_name_source == "Upstream Co"
    assert row.downstream_customer_name_source == "Downstream Co"
    assert row.revenue == Decimal("1942.51")
    assert row.cost == Decimal("1155.14")
    assert row.text_values[20] == "004047806"
    assert row.integer_values[21] == 42
    assert row.decimal_values[22] == Decimal("1.25")
    assert row.date_values[23] == date(2026, 10, 18)
    assert row.boolean_values[24] is True
    assert row.enum_option_ids[25] == 101


def test_excel_text_preserves_exact_characters(tmp_path: Path) -> None:
    values = ("004047806", "  spaced id  ", "数据 001", "0", "'quoted'")
    for index, value in enumerate(values):
        path = write_rows_workbook(
            tmp_path,
            [excel_row(**{"Text ID": value})],
            filename=f"text-{index}.xlsx",
        )
        row = single_valid(path)
        assert row.text_values[20] == value


def test_excel_eight_digit_date_and_zero_prefixed_id(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Business date": 20261018, "Text ID": "004047806"})],
    )

    results = parse(path)

    assert results[0].issues == ()
    assert results[0].row is not None
    assert results[0].row.business_date == date(2026, 10, 18)
    assert results[0].row.text_values[20] == "004047806"


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 18),
        date(2026, 10, 18),
        "2026-10-18",
        " 2026-10-18 ",
        20261018,
        "20261018",
        " 20261018 ",
        # Excel stores all numbers as doubles; an integral double is the same
        # cell value as the integer form.
        20261018.0,
    ],
)
def test_excel_accepts_supported_date_forms(tmp_path: Path, value: object) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Business date": value, "Date": value})],
    )

    row = single_valid(path)

    assert row.business_date == date(2026, 10, 18)
    assert row.date_values[23] == date(2026, 10, 18)


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (datetime(2026, 10, 18, 12, 0), "invalid_date"),
        # Excel stores dates as day serials, so a sub-second time cannot survive
        # a save; whole seconds still must be rejected.
        (datetime(2026, 10, 18, 0, 0, 1), "invalid_date"),
        (time(12, 0), "invalid_date"),
        ("2026-02-30", "invalid_date"),
        (20260230, "invalid_date"),
        (2026021, "invalid_date"),
        ("18/10/2026", "invalid_date"),
        (20261018.5, "invalid_date"),
        (True, "invalid_date"),
        ("٢٠٢٦-١٠-١٨", "invalid_date"),
        ("٢٠٢٦١٠١٨", "invalid_date"),
    ],
)
def test_excel_rejects_time_components_and_illegal_dates(
    tmp_path: Path,
    value: object,
    error_code: str,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Business date": value, "Text ID": "keep"})],
    )

    results = parse(path)

    assert [(item.error_code, item.field_id) for item in results[0].issues] == [
        (error_code, 10)
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("TRUE", True),
        ("FALSE", False),
        (" true ", True),
        ("false", False),
        ("是", True),
        ("否", False),
        ("1", True),
        ("0", False),
        (1, True),
        (0, False),
        # Excel stores numbers as doubles and integral doubles read back as
        # integers, so 1.0 and 1 are the same cell value.
        (1.0, True),
        (0.0, False),
    ],
)
def test_excel_accepts_supported_boolean_forms(
    tmp_path: Path,
    value: object,
    expected: bool,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Boolean": value})],
    )

    row = single_valid(path)

    assert row.boolean_values[24] is expected


@pytest.mark.parametrize("value", ["yes", "T", "2", 2, 2.0, "   ", "真"])
def test_excel_rejects_unknown_boolean_forms(
    tmp_path: Path,
    value: object,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Boolean": value})],
    )

    results = parse(path)

    assert issue_tuples(results[0]) == [("invalid_boolean", 24, "Boolean")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42),
        (-42, -42),
        ("42", 42),
        (" 42 ", 42),
        ("-9223372036854775808", -(2**63)),
        ("9223372036854775807", 2**63 - 1),
        (0, 0),
    ],
)
def test_excel_accepts_native_and_signed_string_integers(
    tmp_path: Path,
    value: object,
    expected: int,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Integer": value})])

    row = single_valid(path)

    assert row.integer_values[21] == expected


def test_excel_native_numbers_are_doubles_above_2_pow_53(tmp_path: Path) -> None:
    """Native Excel numbers cannot represent every int64 exactly.

    A spreadsheet stores numbers as IEEE-754 doubles, so a native cell above
    2**53 is already rounded before this parser sees it. Importing the rounded
    value would be silent corruption, so the integral-double form is converted
    exactly and then range-checked; the full int64 range remains available
    through the decimal-string form.
    """
    exact = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Integer": 2**53})],
        filename="exact.xlsx",
    )
    rounded = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Integer": 2**53 + 1})],
        filename="rounded.xlsx",
    )
    as_text = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Integer": "9223372036854775807"})],
        filename="text.xlsx",
    )

    assert single_valid(exact).integer_values[21] == 2**53
    # 2**53 + 1 is not representable: Excel itself stores 2**53.
    assert single_valid(rounded).integer_values[21] == 2**53
    assert single_valid(as_text).integer_values[21] == 2**63 - 1


def test_excel_int64_boundaries_from_native_cells(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Integer": -(2**63)})],
        filename="min.xlsx",
    )

    assert single_valid(path).integer_values[21] == -(2**63)

    overflow = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Integer": 2**63})],
        filename="overflow.xlsx",
    )
    results = parse(overflow)
    assert issue_tuples(results[0]) == [("integer_out_of_range", 21, "Integer")]


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (1.5, "invalid_integer"),
        (1e20, "integer_out_of_range"),
        ("1.0", "invalid_integer"),
        ("9223372036854775808", "integer_out_of_range"),
        (9223372036854775808, "integer_out_of_range"),
        ("9" * 4_301, "integer_out_of_range"),
        (True, "invalid_integer"),
        ("٢٠", "invalid_integer"),
    ],
)
def test_excel_rejects_non_integer_cells(
    tmp_path: Path,
    value: object,
    error_code: str,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Integer": value})])

    results = parse(path)

    assert issue_tuples(results[0]) == [(error_code, 21, "Integer")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.25, Decimal("1.25")),
        (1942.51, Decimal("1942.51")),
        ("1.2300", Decimal("1.2300")),
        (5, Decimal("5")),
        (5.0, Decimal("5.0")),
        ("-0.00000000", Decimal("-0.00000000")),
    ],
)
def test_excel_accepts_native_numeric_and_string_decimals(
    tmp_path: Path,
    value: object,
    expected: Decimal,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Revenue": value, "Decimal": value})],
    )

    row = single_valid(path)

    assert row.revenue == expected
    assert row.decimal_values[22] == expected
    assert type(row.revenue) is Decimal
    assert type(row.decimal_values[22]) is Decimal


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (1.234567891, "too_many_decimal_places"),
        (1e-9, "too_many_decimal_places"),
        ("1.000000001", "too_many_decimal_places"),
        (1e22, "decimal_out_of_range"),
        (1e300, "decimal_out_of_range"),
        (True, "invalid_decimal"),
        ("abc", "invalid_decimal"),
    ],
)
def test_excel_rejects_invalid_decimal_cells(
    tmp_path: Path,
    value: object,
    error_code: str,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Revenue": value})])

    results = parse(path)

    assert issue_tuples(results[0]) == [(error_code, 13, "Revenue")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Direct", 101),
        (" Direct ", 101),
        ("Indirect", 102),
    ],
)
def test_excel_enum_matches_trimmed_exact_label(
    tmp_path: Path,
    value: str,
    expected: int,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Channel": value})])

    row = single_valid(path)

    assert row.enum_option_ids[25] == expected


@pytest.mark.parametrize("value", ["direct", "Directt", 1])
def test_excel_enum_rejects_case_and_type_mismatch(
    tmp_path: Path,
    value: object,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Channel": value})])

    results = parse(path)

    assert issue_tuples(results[0]) == [("invalid_enum", 25, "Channel")]


def test_excel_customer_names_are_matched_like_mysql_collation(
    tmp_path: Path,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [
            excel_row(
                **{
                    "Upstream": "  upstream CO ",
                    "Downstream": "CAFE ALPHA",
                    "Channel": None,
                }
            )
        ],
    )

    row = single_valid(path)

    assert row.upstream_customer_id == 101
    assert row.downstream_customer_id == 303
    assert row.upstream_customer_name_source == "Upstream Co"
    assert row.downstream_customer_name_source == "Café Alpha"


@pytest.mark.parametrize(
    "value",
    ["Missing Co", "Deleted Co", "   ", 101, True],
)
def test_excel_rejects_unknown_deleted_or_non_text_customers(
    tmp_path: Path,
    value: object,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Upstream": value})])

    results = parse(path)

    assert [(issue.error_code, issue.field_id) for issue in results[0].issues] == [
        ("invalid_customer", 11)
    ]
    assert "Missing Co" not in results[0].issues[0].message
    assert "Deleted Co" not in results[0].issues[0].message


def test_excel_empty_customer_cell_is_required(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Upstream": None})])

    results = parse(path)

    assert issue_tuples(results[0]) == [("required", 11, "Upstream")]


def test_excel_whitespace_customer_cell_is_not_a_name(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row(**{"Upstream": "   "})])

    results = parse(path)

    assert issue_tuples(results[0]) == [("invalid_customer", 11, "Upstream")]


def test_excel_reports_both_invalid_customer_columns(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Upstream": "Missing One", "Downstream": "Missing Two"})],
    )

    results = parse(path)

    assert [(issue.error_code, issue.field_id) for issue in results[0].issues] == [
        ("invalid_customer", 11),
        ("invalid_customer", 12),
    ]
    assert "Missing" not in " ".join(issue.message for issue in results[0].issues)


@pytest.mark.parametrize(
    ("allowed", "field_ids"),
    [
        ({202}, [11]),
        ({101}, [12]),
        (set(), [11, 12]),
    ],
)
def test_excel_limited_scope_requires_both_customers(
    tmp_path: Path,
    allowed: set[int],
    field_ids: list[int],
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    results = parse(path, allowed_customer_ids=allowed)

    assert [(issue.error_code, issue.field_id) for issue in results[0].issues] == [
        ("customer_out_of_scope", field_id) for field_id in field_ids
    ]


def test_excel_accepts_both_customers_inside_allowed_scope(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    results = parse(path, allowed_customer_ids={101, 202})

    assert results[0].issues == ()
    assert results[0].row is not None
    assert results[0].row.upstream_customer_id == 101


def test_excel_builds_customer_id_index_once(tmp_path: Path) -> None:
    customer_index = CountingCustomerIndex(make_customer_index())
    path = write_rows_workbook(
        tmp_path,
        [
            excel_row(),
            excel_row(**{"Text ID": "second"}),
        ],
    )

    results = parse(path, customer_index=customer_index)

    assert len(results) == 2
    assert all(result.issues == () for result in results)
    assert customer_index.values_calls == 1


def test_excel_blank_cells_are_absent_optional_values(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Boolean": None, "Integer": None, "Channel": None})],
    )

    row = single_valid(path)

    assert 21 not in row.integer_values
    assert 24 not in row.boolean_values
    assert 25 not in row.enum_option_ids


def test_excel_text_keeps_formula_shaped_characters(tmp_path: Path) -> None:
    values = ("=1+1", "=note", "==", "=SUM(A1:A2)")
    for index, value in enumerate(values):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Data"
        worksheet.append(list(DEFINITION.labels))
        row_values = [excel_row().get(label) for label in DEFINITION.labels]
        row_values[DEFINITION.labels.index("Text ID")] = value
        worksheet.append(row_values)
        text_column = DEFINITION.labels.index("Text ID") + 1
        text_cell = worksheet.cell(row=2, column=text_column)
        # Pin an explicit text cell: openpyxl would otherwise store a leading
        # "=" as a formula, which is exactly the case under test.
        text_cell.data_type = "s"
        path = tmp_path / f"text-formula-{index}.xlsx"
        workbook.save(path)
        workbook.close()

        assert single_valid(path).text_values[20] == value


def test_excel_rejects_numeric_text_cell(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Text ID": 123})],
    )

    results = parse(path)

    assert issue_tuples(results[0]) == [("invalid_text", 20, "Text ID")]


def test_excel_enforces_text_length_limit(tmp_path: Path) -> None:
    accepted = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Text ID": "x" * 4_000})],
        filename="accepted.xlsx",
    )
    rejected = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Text ID": "y" * 4_001})],
        filename="rejected.xlsx",
    )

    assert single_valid(accepted).text_values[20] == "x" * 4_000
    results = parse(rejected)
    assert issue_tuples(results[0]) == [("text_too_long", 20, "Text ID")]
    assert "y" * 32 not in results[0].issues[0].message


# ---------------------------------------------------------------------------
# Header mapping and row structure
# ---------------------------------------------------------------------------


def test_excel_header_order_is_independent(tmp_path: Path) -> None:
    headers = list(reversed(DEFINITION.labels))
    path = write_rows_workbook(tmp_path, [excel_row()], headers=headers)

    row = single_valid(path)

    assert row.business_date == date(2026, 10, 18)
    assert row.text_values[20] == "004047806"
    assert row.enum_option_ids[25] == 101


def test_excel_rejects_value_in_column_past_last_label(tmp_path: Path) -> None:
    header: list[object | None] = [*DEFINITION.labels, None]
    row: list[object | None] = [excel_row().get(label) for label in DEFINITION.labels]
    row.append("stray")
    path = write_raw_workbook(tmp_path, [header, row])

    assert_invalid_excel(
        path,
        "Excel row contains values outside the configured columns",
    )


def test_excel_rejects_blank_header_column_inside_labels(tmp_path: Path) -> None:
    header: list[object | None] = [*DEFINITION.labels, None]
    header[3] = None
    row: list[object | None] = [excel_row().get(label) for label in DEFINITION.labels]
    row.insert(3, None)
    path = write_raw_workbook(tmp_path, [header, row])

    assert_invalid_excel(path, "Excel header row is invalid")


def test_excel_header_trim_matches_configured_labels(tmp_path: Path) -> None:
    padded = [f"  {label}  " for label in DEFINITION.labels]
    values = [excel_row().get(label) for label in DEFINITION.labels]
    path = write_raw_workbook(tmp_path, [padded, values])

    assert single_valid(path).integer_values[21] == 42


INVALID_HEADER_CASES: tuple[tuple[str, list[object]], ...] = (
    ("missing", list(DEFINITION.labels[:-1])),
    ("unknown", [*DEFINITION.labels[:-1], "Unknown"]),
    ("duplicate", [*DEFINITION.labels[:-1], DEFINITION.labels[0]]),
    ("blank-after", [*DEFINITION.labels[:-1], None]),
    ("non-string", [*DEFINITION.labels[:-1], 1]),
    ("case-mismatch", [*DEFINITION.labels[:-1], " text id "]),
    ("empty-label", ["", *DEFINITION.labels[1:]]),
)


@pytest.mark.parametrize(
    "headers",
    [headers for _, headers in INVALID_HEADER_CASES],
    ids=[label for label, _ in INVALID_HEADER_CASES],
)
def test_excel_rejects_invalid_headers(
    tmp_path: Path,
    headers: list[object],
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()], headers=headers)

    error = assert_invalid_excel(path, "Excel header row is invalid")

    for label in headers:
        if isinstance(label, str) and label.strip():
            assert label not in error.message


def test_excel_rejects_header_not_in_first_row(tmp_path: Path) -> None:
    path = write_raw_workbook(
        tmp_path,
        [
            list(excel_row().values()),
            list(DEFINITION.labels),
        ],
    )

    assert_invalid_excel(path, "Excel header row is invalid")


def test_excel_rejects_empty_workbook(tmp_path: Path) -> None:
    path = write_raw_workbook(tmp_path, [])

    assert_invalid_excel(path, "Excel header row is invalid")


def test_excel_header_only_workbook_yields_no_rows(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [])

    assert parse(path) == []


def test_excel_ignores_empty_rows_and_preserves_sheet_row_numbers(
    tmp_path: Path,
) -> None:
    path = write_rows_workbook(
        tmp_path,
        [
            excel_row(),
            excel_row(**{"Text ID": None, "Revenue": None}),
            excel_row(**{"Text ID": "row-four"}),
            {label: None for label in DEFINITION.labels},
            excel_row(**{"Text ID": "   "}),
        ],
    )

    results = parse(path)

    assert [result.row_number for result in results] == [2, 3, 4, 6]
    assert issue_tuples(results[1]) == [
        ("required", 13, "Revenue"),
        ("required", 20, "Text ID"),
    ]
    assert all(issue.sheet_name == "Data" for issue in results[1].issues)
    assert all(issue.row_number == 3 for issue in results[1].issues)
    assert results[3].row is not None
    assert results[3].row.text_values[20] == "   "


def test_excel_issues_carry_sheet_row_column_and_field_id(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Revenue": "secret", "Channel": "nope"})],
        title="Detail Sheet",
    )

    results = parse(path)

    assert [
        (
            issue.error_code,
            issue.field_id,
            issue.column_label,
            issue.sheet_name,
            issue.row_number,
        )
        for issue in results[0].issues
    ] == [
        ("invalid_decimal", 13, "Revenue", "Detail Sheet", 2),
        ("invalid_enum", 25, "Channel", "Detail Sheet", 2),
    ]
    assert "secret" not in results[0].issues[0].message
    assert "nope" not in results[0].issues[1].message


def test_excel_early_stop_releases_workbook(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [
            excel_row(**{"Revenue": "bad"}),
            excel_row(**{"Text ID": "second"}),
        ],
    )

    rows = iter_excel_rows(path, DEFINITION, make_customer_index(), None)
    first = next(rows)
    assert issue_tuples(first) == [("invalid_decimal", 13, "Revenue")]
    rows.close()

    # The workbook became reusable: a full parse still sees every row.
    assert [result.row_number for result in parse(path)] == [2, 3]


def test_excel_rejects_files_over_50_mib(tmp_path: Path) -> None:
    path = tmp_path / "oversized.xlsx"
    path.write_bytes(b"PK\x03\x04" + b"0" * (50 * 1024 * 1024))

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_values_outside_configured_columns(tmp_path: Path) -> None:
    header = [*DEFINITION.labels, None]
    row: list[object | None] = [excel_row().get(label) for label in DEFINITION.labels]
    path = write_raw_workbook(tmp_path, [header, [*row, "stray"]])

    assert_invalid_excel(
        path,
        "Excel row contains values outside the configured columns",
    )


def test_excel_ignores_styled_but_blank_extra_columns(tmp_path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    worksheet.append(list(DEFINITION.labels))
    worksheet.append([excel_row().get(label) for label in DEFINITION.labels])
    worksheet["A1"].font = Font(bold=True)
    worksheet["M2"].font = Font(bold=True)
    path = tmp_path / "styled.xlsx"
    workbook.save(path)
    workbook.close()

    row = single_valid(path)

    assert row.text_values[20] == "004047806"


# ---------------------------------------------------------------------------
# Containers, sheets, formulas, and limits
# ---------------------------------------------------------------------------


def test_excel_rejects_data_formulas(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Revenue": "=SUM(1,2)"})],
    )

    assert_invalid_excel(path, "Excel formulas are not supported")


def test_excel_rejects_formulas_with_cached_values(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Revenue": "=1+1"})],
    )

    def add_cached_value(target: MutableZip) -> None:
        sheet_xml = target.read("xl/worksheets/sheet1.xml")
        assert b"</f>" in sheet_xml
        target.writestr(
            "xl/worksheets/sheet1.xml",
            sheet_xml.replace(b"</f>", b"</f><v>2</v>", 1),
        )

    rewrite_zip(path, add_cached_value)
    cached = load_workbook(path, read_only=True, data_only=True)
    try:
        cached_rows = list(cached.worksheets[0].iter_rows(values_only=True))
    finally:
        cached.close()
    assert cached_rows[1][3] == 2

    assert_invalid_excel(path, "Excel formulas are not supported")


def test_excel_rejects_header_formulas(tmp_path: Path) -> None:
    headers = list(DEFINITION.labels)
    headers[0] = "=1+1"
    path = write_rows_workbook(tmp_path, [excel_row()], headers=headers)

    assert_invalid_excel(path, "Excel formulas are not supported")


def test_excel_rejects_formulas_outside_configured_columns(tmp_path: Path) -> None:
    header = [*DEFINITION.labels, None]
    row: list[object | None] = [excel_row().get(label) for label in DEFINITION.labels]
    row.append("=1+1")
    path = write_raw_workbook(tmp_path, [header, row])

    assert_invalid_excel(path, "Excel formulas are not supported")


def test_excel_rejects_error_value_cells(tmp_path: Path) -> None:
    path = write_rows_workbook(
        tmp_path,
        [excel_row(**{"Revenue": "#DIV/0!"})],
    )

    assert_invalid_excel(path, "Excel error values are not supported")


def test_excel_rejects_multiple_worksheets(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()], extra_sheet=True)

    assert_invalid_excel(
        path,
        "Excel file must contain exactly one visible worksheet",
    )


@pytest.mark.parametrize("sheet_state", ["hidden", "veryHidden"])
def test_excel_rejects_hidden_worksheet(
    tmp_path: Path,
    sheet_state: str,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])
    set_workbook_sheet_state(path, sheet_state)

    assert_invalid_excel(
        path,
        "Excel file must contain exactly one visible worksheet",
    )


def test_excel_rejects_chartsheet_outside_standard_part_path(
    tmp_path: Path,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()], chartsheet=True)

    def move_parts(target: MutableZip) -> None:
        target.writestr("xl/alt/chart1.xml", target.read("xl/chartsheets/sheet1.xml"))
        del target.entries["xl/chartsheets/sheet1.xml"]
        workbook_rels = target.read("xl/_rels/workbook.xml.rels").replace(
            b"chartsheets/sheet1.xml",
            b"alt/chart1.xml",
        )
        target.writestr("xl/_rels/workbook.xml.rels", workbook_rels)

    rewrite_zip(path, move_parts)

    assert_invalid_excel(
        path,
        "Excel file must contain exactly one visible worksheet",
    )


def test_excel_rejects_chartsheet(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()], chartsheet=True)

    assert_invalid_excel(
        path,
        "Excel file must contain exactly one visible worksheet",
    )


def test_excel_requires_a_worksheet_part(tmp_path: Path) -> None:
    workbook = Workbook()
    chartsheet = workbook.create_chartsheet()
    chartsheet.title = "Chart"
    workbook.remove(workbook["Sheet"])
    path = tmp_path / "charts-only.xlsx"
    workbook.save(path)
    workbook.close()

    assert_invalid_excel(
        path,
        "Excel file must contain exactly one visible worksheet",
    )


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not an xlsx file",
        b"PK\x03\x04truncated",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-xls",
        b'{"rows": []}',
    ],
    ids=["empty", "text", "truncated-zip", "ole-xls", "json"],
)
def test_excel_rejects_invalid_signatures_and_containers(
    tmp_path: Path,
    content: bytes,
) -> None:
    path = tmp_path / "invalid.xlsx"
    path.write_bytes(content)

    error = assert_invalid_excel(path, "Import Excel file is invalid")
    assert "legacy-xls" not in error.message


def test_excel_rejects_zip_without_workbook_part(tmp_path: Path) -> None:
    path = tmp_path / "missing-workbook.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_workbook_without_relationships(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    def drop_rels(target: MutableZip) -> None:
        del target.entries["xl/_rels/workbook.xml.rels"]

    rewrite_zip(path, drop_rels)

    # A workbook without relationships cannot resolve its worksheet part, so it
    # is rejected as an invalid container.
    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_corrupt_workbook_xml(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    def break_workbook(target: MutableZip) -> None:
        target.writestr("xl/workbook.xml", "<workbook><sheets>")

    rewrite_zip(path, break_workbook)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_macro_enabled_containers(tmp_path: Path) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])

    def add_macro(target: MutableZip) -> None:
        content_types = target.read("[Content_Types].xml").replace(
            b"spreadsheetml.sheet.main+xml",
            b"spreadsheetml.sheet.macroEnabled.main+xml",
        )
        target.writestr("[Content_Types].xml", content_types)
        target.writestr("xl/vbaProject.bin", b"macro")

    rewrite_zip(path, add_macro)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BusinessError) as error:
        parse(tmp_path / "absent.xlsx")

    assert error.value.code == EXCEL_INVALID
    assert error.value.message == "Import Excel file is invalid"


def test_excel_rejects_directory_path(tmp_path: Path) -> None:
    directory = tmp_path / "directory.xlsx"
    directory.mkdir()

    assert_invalid_excel(directory, "Import Excel file is invalid")


def test_excel_rejects_more_than_100000_data_rows(tmp_path: Path) -> None:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Data")
    worksheet.append(list(DEFINITION.labels))
    for _ in range(100_001):
        worksheet.append([datetime(2026, 10, 18)])
    path = tmp_path / "too-many.xlsx"
    workbook.save(path)

    assert_invalid_excel(
        path,
        "Excel file must not exceed 100000 data rows",
    )


def test_excel_accepts_exact_row_limit_with_patched_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import data_import_parser

    monkeypatch.setattr(data_import_parser, "_MAX_ROWS", 3)
    accepted = write_rows_workbook(
        tmp_path,
        [excel_row() for _ in range(3)],
        filename="accepted.xlsx",
    )
    rejected = write_rows_workbook(
        tmp_path,
        [excel_row() for _ in range(4)],
        filename="rejected.xlsx",
    )

    assert len(parse(accepted)) == 3
    assert_invalid_excel(
        rejected,
        "Excel file must not exceed 3 data rows",
    )


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def test_template_has_exact_single_header_row() -> None:
    content = build_excel_template(DEFINITION)

    assert content.startswith(b"PK")
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    try:
        assert len(workbook.worksheets) == 1
        assert workbook.chartsheets == []
        worksheet = workbook.worksheets[0]
        assert worksheet.sheet_state == "visible"
        rows = list(worksheet.iter_rows(values_only=True))
    finally:
        workbook.close()

    assert len(rows) == 1
    assert tuple(rows[0]) == DEFINITION.labels
    assert all(isinstance(value, str) for value in rows[0])


def test_template_is_a_valid_empty_import_sheet(tmp_path: Path) -> None:
    path = tmp_path / "template.xlsx"
    path.write_bytes(build_excel_template(DEFINITION))

    assert parse(path) == []


def test_template_preserves_unicode_labels() -> None:
    definition = make_unicode_definition()
    content = build_excel_template(definition)

    workbook = load_workbook(io.BytesIO(content), read_only=True)
    try:
        assert len(workbook.worksheets) == 1
        rows = list(workbook.worksheets[0].iter_rows(values_only=True))
    finally:
        workbook.close()

    assert tuple(rows[0]) == definition.labels
    assert "备注" in rows[0]


def test_template_has_no_formulas_or_example_rows() -> None:
    content = build_excel_template(DEFINITION)

    workbook = load_workbook(io.BytesIO(content), data_only=False)
    try:
        worksheet = workbook.active
        assert worksheet.max_row == 1
        assert worksheet.max_column == len(DEFINITION.labels)
        for row in worksheet.iter_rows():
            for cell in row:
                assert cell.data_type != "f"
                assert cell.value is not None
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# Hardened container boundaries: decompression, coordinates, macros
# ---------------------------------------------------------------------------


def archive_entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as source:
        return {name: source.read(name) for name in source.namelist()}


def repack_entries(path: Path, entries: Mapping[str, bytes]) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in entries.items():
            target.writestr(name, content)
    path.write_bytes(buffer.getvalue())
    return path


def patch_worksheet_xml(path: Path, old: bytes, new: bytes) -> Path:
    entries = archive_entries(path)
    worksheet_xml = entries["xl/worksheets/sheet1.xml"]
    assert old in worksheet_xml, old
    entries["xl/worksheets/sheet1.xml"] = worksheet_xml.replace(old, new, 1)
    return repack_entries(path, entries)


def write_worksheet_member_stored(path: Path, worksheet_xml: bytes) -> Path:
    """Rewrite the worksheet without compression so ratio limits stay irrelevant."""
    entries = archive_entries(path)
    worksheet_name = "xl/worksheets/sheet1.xml"
    entries[worksheet_name] = worksheet_xml
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for name, content in entries.items():
            method = (
                zipfile.ZIP_STORED if name == worksheet_name else zipfile.ZIP_DEFLATED
            )
            target.writestr(name, content, compress_type=method)
    path.write_bytes(buffer.getvalue())
    return path


def forbid_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if any archive member is expanded after a rejection."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("archive member was expanded before rejection")

    monkeypatch.setattr(zipfile.ZipFile, "read", explode)
    monkeypatch.setattr(zipfile.ZipFile, "open", explode)


def forbid_member_expansion(
    monkeypatch: pytest.MonkeyPatch,
    members: Sequence[str],
) -> None:
    """Allow preflight metadata reads but reject materializing selected members."""
    from app.services import data_import_parser as parser

    original_read = zipfile.ZipFile.read
    forbidden = frozenset(members)

    def guarded_read(
        archive: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        *args: object,
        **kwargs: object,
    ) -> bytes:
        member = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member in forbidden:
            raise AssertionError("archive member was expanded before rejection")
        return original_read(archive, name, *args, **kwargs)

    def explode_workbook(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("openpyxl was reached before rejection")

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)
    monkeypatch.setattr(parser, "load_workbook", explode_workbook)


def test_xlsx_limits_are_named_and_cover_the_import_envelope() -> None:
    """Pin the chosen limits to the measured 100,000-row x 105-column envelope.

    The numbers are the contract Task 13 will benchmark, so this test states
    why each one is at least as large as the documented workload requires.
    """
    from app.services import data_import_parser as parser

    # 100,000 rows x 105 columns of varying short text measured 578 MiB.
    assert parser._XLSX_MAX_WORKSHEET_BYTES >= 578 * 1024 * 1024
    # 100,000 rows x 100 unique text fields measured about 460 MiB.
    assert parser._XLSX_MAX_METADATA_BYTES >= 460 * 1024 * 1024
    # The aggregate budget must not make the worksheet budget unreachable.
    assert (
        parser._XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES
        >= parser._XLSX_MAX_WORKSHEET_BYTES + parser._XLSX_MAX_METADATA_BYTES
    )
    # A data row may be followed by at most one blank separator row.
    assert parser._MAX_ROWS == 100_000
    assert parser._XLSX_MAX_SPARSE_ROW == parser._MAX_ROWS * 2
    # 100 custom fields plus five system fields are always addressable.
    assert parser._XLSX_MAX_SPARSE_COLUMN >= 5 + 100
    assert parser._XLSX_MAX_ROW_CHILDREN >= 5 + 100
    assert parser._XLSX_SPARSE_ROW_SLACK == 2
    # 424.5:1 is the measured worst legitimate case (4,000 identical
    # characters per cell); 1,032:1 is deflate's physical ceiling for zeros.
    assert 424.5 < parser._XLSX_MAX_COMPRESSION_RATIO < 1_032
    assert parser._XLSX_RATIO_MIN_BYTES >= 1024 * 1024
    assert parser._XLSX_ROW_LIMIT == 1_048_576
    assert parser._XLSX_COLUMN_LIMIT == 16_384


def test_excel_rejects_decompression_bomb_before_any_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 96 MiB all-zero worksheet compresses to a few KiB; metadata rejects it.

    The file stays well under the 50 MiB compressed limit, so only the
    compression-ratio gate can see the bomb, and it must decide from the ZIP
    central directory without decompressing a single member.
    """
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    entries["xl/worksheets/sheet1.xml"] = b"\x00" * (96 * 1024 * 1024)
    repack_entries(path, entries)
    assert path.stat().st_size < 1024 * 1024

    forbid_expansion(monkeypatch)

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    "member",
    [
        pytest.param("xl/sharedStrings.xml", id="normal-name"),
        pytest.param("xl/sharedStrings/", id="shared-strings-directory-name"),
        pytest.param("xl/worksheets/", id="worksheets-directory-name"),
    ],
)
def test_excel_budgets_32_mib_bomb_under_any_member_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    """Directory-looking names do not change a member's expansion budget."""
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    entries[member] = b"\x00" * (32 * 1024 * 1024)
    repack_entries(path, entries)

    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        assert info.is_dir() is member.endswith("/")
        assert info.file_size / info.compress_size >= 1025

    forbid_expansion(monkeypatch)

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    "member",
    [
        pytest.param("xl/budget-probe", id="normal-name"),
        pytest.param("xl/budget-probe/", id="directory-name"),
    ],
)
def test_excel_budgets_over_limit_member_regardless_of_directory_style_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    """The metadata cap compares identical normal and trailing-slash members.

    The cap is above the largest untouched non-worksheet package member, so the
    added payload is the only per-member budget violation in both variants.
    """
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    worksheet_name = "xl/worksheets/sheet1.xml"
    legitimate_metadata_max = max(
        len(content) for name, content in entries.items() if name != worksheet_name
    )
    payload = b"x" * (legitimate_metadata_max + 64)
    entries[member] = payload
    repack_entries(path, entries)
    cap = len(payload) - 1
    assert cap > legitimate_metadata_max
    monkeypatch.setattr(parser, "_XLSX_MAX_METADATA_BYTES", cap)

    forbid_member_expansion(monkeypatch, (member,))

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    "member",
    [
        pytest.param("xl/budget-probe", id="normal-name"),
        pytest.param("xl/budget-probe/", id="directory-name"),
    ],
)
def test_excel_rejects_aggregate_overage_for_any_directory_style_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    """Trailing slashes cannot change whether an added member crosses the cap."""
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    legitimate_total = sum(len(content) for content in entries.values())
    payload = b"x" * 64
    entries[member] = payload
    repack_entries(path, entries)
    cap = legitimate_total + len(payload) - 1
    assert cap >= legitimate_total
    monkeypatch.setattr(parser, "_XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES", cap)

    forbid_expansion(monkeypatch)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_aggregate_uncompressed_budget_before_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    repack_entries(path, archive_entries(path))
    monkeypatch.setattr(parser, "_XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES", 64)

    forbid_expansion(monkeypatch)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_shared_strings_alias_of_worksheet_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One part cannot be both the streamed worksheet and shared strings.

    openpyxl resolves shared strings from ``[Content_Types].xml`` but resolves
    the worksheet from workbook relationships. Reusing the worksheet target for
    both roles must therefore be rejected structurally before the 900 MiB
    logical part can be materialized as a shared-string table.
    """
    from app.services import data_import_parser as parser

    logical_size = 900 * 1024 * 1024
    logical_compress_size = logical_size // 668
    assert logical_size / logical_compress_size <= 700
    assert logical_size / logical_compress_size >= 668

    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    worksheet_type = (
        b'<Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.'
        b'spreadsheetml.worksheet+xml" />'
    )
    shared_strings_type = (
        b'<Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.'
        b'spreadsheetml.sharedStrings+xml" />'
    )
    assert worksheet_type in entries["[Content_Types].xml"]
    entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
        worksheet_type,
        shared_strings_type,
        1,
    )
    repack_entries(path, entries)

    original_infolist = zipfile.ZipFile.infolist

    def inflated_infolist(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        result = original_infolist(archive)
        for info in result:
            if info.filename == "xl/worksheets/sheet1.xml":
                info.file_size = logical_size
                info.compress_size = logical_compress_size
        return result

    original_open = zipfile.ZipFile.open

    def guarded_open(
        archive: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        *args: object,
        **kwargs: object,
    ) -> object:
        member = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member == "xl/worksheets/sheet1.xml":
            raise AssertionError("aliased part was expanded before rejection")
        return original_open(archive, name, *args, **kwargs)

    def explode_expansion(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("openpyxl was reached before rejection")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", inflated_infolist)
    monkeypatch.setattr(zipfile.ZipFile, "open", guarded_open)
    monkeypatch.setattr(parser, "load_workbook", explode_expansion)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_absolute_worksheet_ceiling_before_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    repack_entries(path, archive_entries(path))
    monkeypatch.setattr(parser, "_XLSX_MAX_WORKSHEET_BYTES", 256)
    monkeypatch.setattr(parser, "_XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES", 1 << 30)

    forbid_expansion(monkeypatch)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_oversized_metadata_part_without_leaking_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared-strings part above the metadata budget is refused, not parsed."""
    from app.services import data_import_parser as parser

    sentinel = b"secret-shared-string-value"
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    entries["xl/sharedStrings.xml"] = b"<sst><si><t>" + sentinel + b"</t></si></sst>"
    repack_entries(path, entries)
    monkeypatch.setattr(parser, "_XLSX_MAX_METADATA_BYTES", 32)

    error = assert_invalid_excel(path, "Import Excel file is invalid")

    assert sentinel.decode() not in error.message


def test_excel_rejects_unsupported_compression_method(tmp_path: Path) -> None:
    """Only STORED and DEFLATED members are accepted, so no other codec runs."""
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for name, content in entries.items():
            method = (
                zipfile.ZIP_BZIP2
                if name == "xl/theme/theme1.xml"
                else zipfile.ZIP_DEFLATED
            )
            target.writestr(name, content, compress_type=method)
    path.write_bytes(buffer.getvalue())

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            b'<dimension ref="A1:K2" />',
            b'<dimension ref="A1:K1048576" />',
            id="declared-max-row",
        ),
        pytest.param(
            b'<dimension ref="A1:K2" />',
            b'<dimension ref="A1:XFD2" />',
            id="declared-max-column",
        ),
        pytest.param(
            b'<dimension ref="A1:K2" />',
            b'<dimension ref="A1:K300000" />',
            id="declared-row-beyond-sparse-budget",
        ),
        pytest.param(
            b'<dimension ref="A1:K2" />',
            b'<dimension ref="A1:IZ2" />',
            id="declared-column-beyond-sparse-budget",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="1048576"><c r="A1048576"><v>1</v></c></row>',
            id="cell-at-max-row",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="2"><c r="XFD2"><v>1</v></c></row>',
            id="cell-at-max-column",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="300000"><c r="A300000"><v>1</v></c></row>',
            id="row-beyond-sparse-budget",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="2"><c r="IZ2"><v>1</v></c></row>',
            id="column-beyond-sparse-budget",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="not-a-number"><c r="A2"><v>1</v></c></row>',
            id="malformed-row-reference",
        ),
        pytest.param(
            b"<sheetData>",
            b'<sheetData><row r="2"><c r="1A"><v>1</v></c></row>',
            id="malformed-cell-reference",
        ),
    ],
)
def test_excel_rejects_sparse_or_oversized_coordinates(
    tmp_path: Path,
    old: bytes,
    new: bytes,
) -> None:
    """A tiny file must not force openpyxl to pad a huge coordinate gap.

    One cell at the XLSX row maximum is a few dozen bytes of XML, but read-only
    iteration would emit a million padded rows for it. The streaming
    coordinate scan rejects it before ``load_workbook`` runs.
    """
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(path, old, new)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_large_declared_dimension_with_little_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared dimension is bounded even when no cell reaches it."""
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(
        path,
        b'<dimension ref="A1:K2" />',
        b'<dimension ref="A1:K199999" />',
    )
    monkeypatch.setattr(parser, "_XLSX_MAX_SPARSE_ROW", 10_000)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_accepts_sparse_rows_within_budget(tmp_path: Path) -> None:
    """Blank separator rows a legitimate sheet declares stay accepted."""
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(
        path,
        b'<dimension ref="A1:K2" />',
        b'<dimension ref="A1:K5000" />',
    )

    assert [result.row_number for result in parse(path)] == [2]


def test_excel_rejects_millions_of_coordinate_less_cells_before_openpyxl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit cell columns are bounded exactly as openpyxl assigns them."""
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    worksheet_name = "xl/worksheets/sheet1.xml"
    worksheet_xml = entries[worksheet_name].replace(
        b"<sheetData>",
        b'<sheetData><row r="2">' + b"<c/>" * 2_000_000 + b"</row>",
        1,
    )
    write_worksheet_member_stored(path, worksheet_xml)
    assert path.stat().st_size < 50 * 1024 * 1024

    def explode_workbook(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("openpyxl was reached before rejection")

    monkeypatch.setattr(parser, "load_workbook", explode_workbook)

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    "child",
    [
        pytest.param(b"<x/>", id="non-cell-row-children"),
        pytest.param(b'<c r="A3"/>', id="repeated-explicit-cell"),
    ],
)
def test_excel_rejects_millions_of_row_children_before_openpyxl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child: bytes,
) -> None:
    """Every direct row child is materialized by openpyxl as a cell."""
    from app.services import data_import_parser as parser

    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    worksheet_name = "xl/worksheets/sheet1.xml"
    worksheet_xml = entries[worksheet_name].replace(
        b"<sheetData>",
        b'<sheetData><row r="3">' + child * 2_000_000 + b"</row>",
        1,
    )
    write_worksheet_member_stored(path, worksheet_xml)
    assert path.stat().st_size < 50 * 1024 * 1024

    def explode_workbook(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("openpyxl was reached before rejection")

    monkeypatch.setattr(parser, "load_workbook", explode_workbook)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_accepts_maximum_width_configured_row(tmp_path: Path) -> None:
    """The row-child bound does not reduce the approved 105-column envelope."""
    definition = make_wide_definition()
    assert len(definition.labels) == 105
    assert len(definition.custom_fields) == 100
    row = excel_row(
        **{
            label: f"value-{index}"
            for index, label in enumerate(definition.labels[11:])
        }
    )
    path = write_rows_workbook(
        tmp_path,
        [row],
        filename="maximum-width.xlsx",
        definition=definition,
    )

    results = parse(path, definition=definition)

    assert len(results) == 1
    assert results[0].issues == ()
    assert results[0].row is not None


def _patch_worksheet_crc(path: Path) -> Path:
    """Flip the worksheet CRC in both the local header and central directory."""
    raw = bytearray(path.read_bytes())
    target = b"xl/worksheets/sheet1.xml"
    # (signature, filename length offset, filename offset, CRC offset)
    headers = (
        (b"PK\x03\x04", 26, 30, 14),
        (b"PK\x01\x02", 28, 46, 16),
    )
    for signature, length_offset, name_offset, crc_offset in headers:
        position = 0
        while True:
            position = raw.find(signature, position)
            assert position >= 0, f"header {signature!r} not found"
            name_length = int.from_bytes(
                raw[position + length_offset : position + length_offset + 2],
                "little",
            )
            name = bytes(
                raw[position + name_offset : position + name_offset + name_length]
            )
            if name == target:
                current = int.from_bytes(
                    raw[position + crc_offset : position + crc_offset + 4],
                    "little",
                )
                raw[position + crc_offset : position + crc_offset + 4] = (
                    current ^ 0xFFFFFFFF
                ).to_bytes(4, "little")
                break
            position += 4
    path.write_bytes(bytes(raw))
    return path


def test_excel_rejects_crc_corrupted_worksheet(tmp_path: Path) -> None:
    """A CRC mismatch is a corrupt workbook, not a raw zipfile exception."""
    path = _patch_worksheet_crc(write_rows_workbook(tmp_path, [excel_row()]))

    with pytest.raises(BusinessError) as error:
        parse(path)

    assert error.value.code == EXCEL_INVALID
    assert error.value.message == "Import Excel file is invalid"
    assert "CRC" not in error.value.message
    assert "sheet1" not in error.value.message


def test_excel_maps_corrupt_shared_string_index_to_static_error(tmp_path: Path) -> None:
    """A lazy shared-string table integrity failure cannot escape raw."""
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    entries["xl/sharedStrings.xml"] = (
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'count="1" uniqueCount="1"><si><t>safe</t></si></sst>'
    )
    entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
        b"</Types>",
        b'<Override PartName="/xl/sharedStrings.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.'
        b'spreadsheetml.sharedStrings+xml" /></Types>',
        1,
    )
    entries["xl/_rels/workbook.xml.rels"] = entries[
        "xl/_rels/workbook.xml.rels"
    ].replace(
        b"</Relationships>",
        b'<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
        b'2006/relationships/sharedStrings" Target="sharedStrings.xml" '
        b'Id="rId99" /></Relationships>',
        1,
    )
    worksheet_xml = entries["xl/worksheets/sheet1.xml"]
    old_cell = b'<c r="F2" t="inlineStr"><is><t>004047806</t></is></c>'
    assert old_cell in worksheet_xml
    entries["xl/worksheets/sheet1.xml"] = worksheet_xml.replace(
        old_cell,
        b'<c r="F2" t="s"><v>999999</v></c>',
        1,
    )
    repack_entries(path, entries)

    with pytest.raises(BusinessError) as error:
        parse(path)

    assert error.value.code == EXCEL_INVALID
    assert error.value.message == "Import Excel file is invalid"
    assert "999999" not in error.value.message
    assert "sharedStrings" not in error.value.message


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            zipfile.BadZipFile("Bad CRC-32 for file 'xl/worksheets/sheet1.xml'"),
            id="bad-zip",
        ),
        pytest.param(zlib.error("incorrect data check"), id="zlib"),
    ],
)
def test_excel_maps_lazy_worksheet_failure_to_static_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """ZIP/decompression errors surfacing mid-iteration become static 9006.

    The preflight passes and the header row is read; the failure is injected on
    the second streamed row, which is exactly where a CRC check or deflate
    error triggered by lazy ``sheetData`` reading would appear.
    """
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    path = write_rows_workbook(tmp_path, [excel_row(), excel_row()])
    original = ReadOnlyWorksheet._cells_by_row
    yielded = 0

    def flaky(self: object, *args: object, **kwargs: object) -> object:
        nonlocal yielded
        for item in original(self, *args, **kwargs):  # type: ignore[arg-type]
            yielded += 1
            if yielded > 1:
                raise failure
            yield item

    monkeypatch.setattr(ReadOnlyWorksheet, "_cells_by_row", flaky)

    with pytest.raises(BusinessError) as error:
        parse(path)

    assert error.value.code == EXCEL_INVALID
    assert error.value.message == "Import Excel file is invalid"
    assert "CRC" not in error.value.message
    assert "sheet1" not in error.value.message


# ---------------------------------------------------------------------------
# Macro detection, warning hygiene, raw numeric dates/booleans, template labels
# ---------------------------------------------------------------------------


def write_macro_variant(
    tmp_path: Path,
    filename: str,
    mutate: Callable[[dict[str, bytes]], None],
) -> Path:
    path = write_rows_workbook(tmp_path, [excel_row()], filename=filename)
    entries = archive_entries(path)
    mutate(entries)
    return repack_entries(path, entries)


def test_excel_rejects_vba_part_under_arbitrary_name(tmp_path: Path) -> None:
    """A VBA part is recognised by its content type, not its filename."""

    def add_vba(entries: dict[str, bytes]) -> None:
        entries["xl/weird/blob.dat"] = b"macro-bytes"
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"</Types>",
            b'<Override PartName="/xl/weird/blob.dat" '
            b'ContentType="application/vnd.ms-office.vbaProject" /></Types>',
        )

    path = write_macro_variant(tmp_path, "arbitrary-vba.xlsx", add_vba)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_vba_relationship_without_vba_part(tmp_path: Path) -> None:
    """A relationship-only VBA declaration is refused even with no member."""

    def add_relationship(entries: dict[str, bytes]) -> None:
        entries["xl/_rels/workbook.xml.rels"] = entries[
            "xl/_rels/workbook.xml.rels"
        ].replace(
            b"</Relationships>",
            b'<Relationship Type="http://schemas.microsoft.com/office/2006/'
            b'relationships/vbaProject" Target="vbaProject.bin" Id="rId99" />'
            b"</Relationships>",
        )

    path = write_macro_variant(tmp_path, "relationship-vba.xlsx", add_relationship)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_macro_enabled_main_content_type(tmp_path: Path) -> None:
    """The macroEnabled main part is refused with no vbaProject part at all."""

    def convert_main(entries: dict[str, bytes]) -> None:
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"spreadsheetml.sheet.main+xml",
            b"spreadsheetml.sheet.macroEnabled.main+xml",
        )

    path = write_macro_variant(tmp_path, "macro-main.xlsx", convert_main)

    assert_invalid_excel(path, "Import Excel file is invalid")


@pytest.mark.parametrize(
    "member",
    [
        "xl/vbaProject.bin",
        "xl/other/vbaProject.bin",
        "xl/macros/VBAPROJECT.bin",
        "xl/vbaProjectSignature.bin",
    ],
)
def test_excel_rejects_any_vba_member_name(tmp_path: Path, member: str) -> None:
    path = write_macro_variant(
        tmp_path,
        "named-vba.xlsx",
        lambda entries: entries.__setitem__(member, b"macro"),
    )

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_vba_content_type_extended_properties(tmp_path: Path) -> None:
    """A non-main, non-bin VBA content type is still a macro workbook."""

    def add_override(entries: dict[str, bytes]) -> None:
        entries["xl/hidden/thing.xml"] = b"<x />"
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"</Types>",
            b'<Override PartName="/xl/hidden/thing.xml" '
            b'ContentType="application/vnd.ms-office.vbaProjectSignature" />'
            b"</Types>",
        )

    path = write_macro_variant(tmp_path, "vba-signature.xlsx", add_override)

    assert_invalid_excel(path, "Import Excel file is invalid")


XLM_MACROSHEET_CONTENT_TYPE = b"application/vnd.ms-excel.macrosheet+xml"
XLM_MACROSHEET_RELATIONSHIP = (
    b"http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"
)


def test_excel_rejects_raw_xlm_macrosheet_package(tmp_path: Path) -> None:
    """A complete macrosheet package remains macro content even without VBA."""

    def add_macrosheet(entries: dict[str, bytes]) -> None:
        entries["xl/macrosheets/sheet1.xml"] = (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/'
            b'spreadsheetml/2006/main"><sheetData /></worksheet>'
        )
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"</Types>",
            b'<Override PartName="/xl/macrosheets/sheet1.xml" ContentType="'
            + XLM_MACROSHEET_CONTENT_TYPE
            + b'" /></Types>',
            1,
        )
        entries["xl/_rels/workbook.xml.rels"] = entries[
            "xl/_rels/workbook.xml.rels"
        ].replace(
            b"</Relationships>",
            b'<Relationship Type="'
            + XLM_MACROSHEET_RELATIONSHIP
            + b'" Target="macrosheets/sheet1.xml" Id="rId99" /></Relationships>',
            1,
        )

    path = write_macro_variant(tmp_path, "xlm-macrosheet.xlsx", add_macrosheet)

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_macrosheet_content_type_without_part(tmp_path: Path) -> None:
    """The content-type declaration alone identifies executable macro content."""

    def declare_content_type(entries: dict[str, bytes]) -> None:
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"</Types>",
            b'<Override PartName="/xl/missing/xlm.xml" ContentType="'
            + XLM_MACROSHEET_CONTENT_TYPE
            + b'" /></Types>',
            1,
        )

    path = write_macro_variant(
        tmp_path,
        "xlm-content-type.xlsx",
        declare_content_type,
    )

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_rejects_macrosheet_relationship_without_part(tmp_path: Path) -> None:
    """The xlMacrosheet relationship alone is enough to reject the workbook."""

    def declare_relationship(entries: dict[str, bytes]) -> None:
        entries["xl/_rels/workbook.xml.rels"] = entries[
            "xl/_rels/workbook.xml.rels"
        ].replace(
            b"</Relationships>",
            b'<Relationship Type="'
            + XLM_MACROSHEET_RELATIONSHIP
            + b'" Target="missing-xlm.xml" Id="rId99" /></Relationships>',
            1,
        )

    path = write_macro_variant(
        tmp_path,
        "xlm-relationship.xlsx",
        declare_relationship,
    )

    assert_invalid_excel(path, "Import Excel file is invalid")


XL_INTL_MACROSHEET_RELATIONSHIP = (
    b"http://schemas.microsoft.com/office/2006/relationships/intlMacrosheet"
)


def test_excel_rejects_intl_macrosheet_relationship_with_benign_part(
    tmp_path: Path,
) -> None:
    """IntlMacrosheet is executable even when its target looks like a worksheet."""

    def declare_intl_relationship(entries: dict[str, bytes]) -> None:
        entries["xl/intl-benign.xml"] = b"<Properties />"
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"</Types>",
            b'<Override PartName="/xl/intl-benign.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.'
            b'extended-properties+xml" /></Types>',
            1,
        )
        entries["xl/_rels/workbook.xml.rels"] = entries[
            "xl/_rels/workbook.xml.rels"
        ].replace(
            b"</Relationships>",
            b'<Relationship Type="'
            + XL_INTL_MACROSHEET_RELATIONSHIP
            + b'" Target="intl-benign.xml" Id="rId99" /></Relationships>',
            1,
        )

    path = write_macro_variant(
        tmp_path,
        "intl-macrosheet-relationship.xlsx",
        declare_intl_relationship,
    )

    assert_invalid_excel(path, "Import Excel file is invalid")


def test_excel_keeps_accepting_macro_free_workbooks(tmp_path: Path) -> None:
    """The macro checks reject only evidence of macros, not ordinary sheets."""
    path = write_rows_workbook(tmp_path, [excel_row()])

    assert single_valid(path).text_values[20] == "004047806"


SENTINEL_DATE_SERIAL = 987654321.75
SENTINEL_BOOLEAN_NUMBER = 42.5


@pytest.mark.parametrize(
    ("replacement", "expected_message"),
    [
        pytest.param(
            (
                b'<c r="A2" s="1" t="n"><v>46313</v></c>',
                b'<c r="A2" s="1" t="n"><v>987654321.75</v></c>',
            ),
            "Excel error values are not supported",
            id="out-of-range-date-serial",
        ),
        pytest.param(
            (
                b'<c r="G2" t="n"><v>42</v></c>',
                b'<c r="G2" t="n"><v>42.5</v></c>',
            ),
            None,
            id="non-integral-integer",
        ),
        pytest.param(
            (
                b'<c r="J2" t="b"><v>1</v></c>',
                b'<c r="J2" t="n"><v>42.5</v></c>',
            ),
            None,
            id="non-integral-boolean",
        ),
    ],
)
def test_excel_warnings_never_leak_raw_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    replacement: tuple[bytes, bytes],
    expected_message: str | None,
) -> None:
    """openpyxl warnings must not reach logs, and the error must stay static.

    A date-styled cell holding an out-of-range serial makes openpyxl warn with
    the coordinate *and the raw value* while the worksheet is lazily streamed.
    That warning must be suppressed, and neither the log record nor the raised
    error may contain the sentinel.
    """
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(path, *replacement)
    sentinels = ("987654321.75", "42.5")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with caplog.at_level(logging.DEBUG):
            try:
                results = parse(path)
            except BusinessError as error:
                assert error.code == EXCEL_INVALID
                if expected_message is not None:
                    assert error.message == expected_message
                messages = [error.message]
            else:
                messages = [
                    issue.message for result in results for issue in result.issues
                ]

    for text in messages:
        for sentinel in sentinels:
            assert sentinel not in text
    for record in caplog.records:
        for sentinel in sentinels:
            assert sentinel not in record.getMessage()
    for warning in caught:
        for sentinel in sentinels:
            assert sentinel not in str(warning.message)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(
            b'<c r="I2" s="1" t="n"><v>46313</v></c>',
            b'<c r="I2" t="n"><v>20261018.0</v></c>',
            date(2026, 10, 18),
            id="date-integral-double",
        ),
        pytest.param(
            b'<c r="I2" s="1" t="n"><v>46313</v></c>',
            b'<c r="I2" t="n"><v>20261018</v></c>',
            date(2026, 10, 18),
            id="date-integer",
        ),
    ],
)
def test_excel_accepts_raw_integral_float_dates(
    tmp_path: Path,
    old: bytes,
    new: bytes,
    expected: date,
) -> None:
    """Raw ``<v>20261018.0</v>`` is a compact date, exactly like ``20261018``.

    openpyxl's ``_cast_number`` promotes any value containing ``"."`` to float,
    so a compact date written with a decimal point arrives as ``20261018.0``.
    """
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(path, old, new)

    assert single_valid(path).date_values[23] == expected


@pytest.mark.parametrize(
    ("new", "expected"),
    [
        (b'<c r="I2" t="n"><v>20261018.5</v></c>', "invalid_date"),
        (b'<c r="I2" t="n"><v>1e300</v></c>', "invalid_date"),
        (b'<c r="I2" t="n"><v>-20261018.0</v></c>', "invalid_date"),
    ],
)
def test_excel_rejects_raw_non_integral_float_dates(
    tmp_path: Path,
    new: bytes,
    expected: str,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(
        path,
        b'<c r="I2" s="1" t="n"><v>46313</v></c>',
        new,
    )

    results = parse(path)

    assert issue_tuples(results[0]) == [(expected, 23, "Date")]


@pytest.mark.parametrize(
    ("new", "expected"),
    [
        (b'<c r="J2" t="n"><v>1.0</v></c>', True),
        (b'<c r="J2" t="n"><v>0.0</v></c>', False),
    ],
)
def test_excel_accepts_raw_float_booleans(
    tmp_path: Path,
    new: bytes,
    expected: bool,
) -> None:
    """Raw ``<v>1.0</v>``/``<v>0.0</v>`` are Excel's numeric boolean form."""
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(path, b'<c r="J2" t="b"><v>1</v></c>', new)

    assert single_valid(path).boolean_values[24] is expected


@pytest.mark.parametrize(
    "new",
    [
        b'<c r="J2" t="n"><v>1.5</v></c>',
        b'<c r="J2" t="n"><v>2.0</v></c>',
        b'<c r="J2" t="n"><v>-1.0</v></c>',
        b'<c r="J2" t="n"><v>0.5</v></c>',
    ],
)
def test_excel_rejects_raw_fractional_or_non_boolean_floats(
    tmp_path: Path,
    new: bytes,
) -> None:
    path = write_rows_workbook(tmp_path, [excel_row()])
    patch_worksheet_xml(path, b'<c r="J2" t="b"><v>1</v></c>', new)

    results = parse(path)

    assert issue_tuples(results[0]) == [("invalid_boolean", 24, "Boolean")]


def make_formula_label_definition() -> ResolvedDataDefinition:
    """The standard definition with formula-shaped labels in two columns.

    ``_field`` rebuilds each field so the shared test definition stays
    immutable; only the label changes.
    """
    labels = {10: "=SUM(A1)", 20: "=1+1"}
    fields = tuple(
        _field(
            field.id,
            labels.get(field.id, field.label),
            field.field_type,
            field.system_key,
            required=field.required,
            sort_order=field.sort_order,
        )
        for field in DEFINITION.fields
    )
    return ResolvedDataDefinition(
        table=DEFINITION.table,
        fields=fields,
        fields_by_id={field.id: field for field in fields},
        fields_by_system_key={
            field.system_key: field
            for field in fields
            if field.system_key != DataFieldRole.CUSTOM
        },
        enum_options_by_id=DEFINITION.enum_options_by_id,
        enum_options_by_field_id=DEFINITION.enum_options_by_field_id,
    )


def test_template_keeps_formula_shaped_labels_as_strings() -> None:
    """A label beginning with ``=`` must be a literal string cell, not a formula."""
    definition = make_formula_label_definition()

    content = build_excel_template(definition)

    workbook = load_workbook(io.BytesIO(content), data_only=False)
    try:
        worksheet = workbook.active
        headers = list(worksheet[1])
        assert [cell.value for cell in headers] == list(definition.labels)
        assert all(cell.data_type == "s" for cell in headers)
        assert all(cell.data_type != "f" for cell in headers)
    finally:
        workbook.close()


def test_template_with_formula_shaped_labels_round_trips(
    tmp_path: Path,
) -> None:
    """The template the operator receives must import back unchanged."""
    definition = make_formula_label_definition()
    path = tmp_path / "formula-label-template.xlsx"
    path.write_bytes(build_excel_template(definition))

    assert parse(path, definition=definition) == []

    entries = archive_entries(path)
    worksheet_xml = entries["xl/worksheets/sheet1.xml"]
    # The label text is preserved verbatim, but never as a formula element.
    assert b"<f>" not in worksheet_xml
    assert b"<f " not in worksheet_xml
    assert b'<c r="A1" s="1" t="inlineStr"><is><t>=SUM(A1)</t></is></c>' in (
        worksheet_xml
    )
    assert b'<c r="F1" s="1" t="inlineStr"><is><t>=1+1</t></is></c>' in worksheet_xml


def test_openpyxl_warning_suppression_is_scoped_to_openpyxl() -> None:
    """Suppression must silence openpyxl only, never unrelated warnings.

    openpyxl warns with the raw serial value of an out-of-range date cell; the
    same mechanism must not swallow a warning emitted by our own modules.
    ``warn_explicit`` pins the emitting module name, which is exactly what the
    filter matches on.
    """
    from app.services.data_import_parser import _suppress_openpyxl_warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _suppress_openpyxl_warnings():
            warnings.warn_explicit(
                "Cell A1 is marked as a date but the serial value 987654321.75",
                UserWarning,
                "sheet.py",
                1,
                module="openpyxl.worksheet._reader",
            )
            warnings.warn_explicit(
                "application sentinel",
                UserWarning,
                "service.py",
                1,
                module="app.services.data_import_storage",
            )

    messages = [str(warning.message) for warning in caught]
    assert "application sentinel" in messages
    assert all("987654321.75" not in message for message in messages)


def test_excel_rejects_worksheet_part_mismatch_after_preflight(tmp_path: Path) -> None:
    """The member openpyxl streams must be the member the preflight bounded.

    A workbook can declare its single worksheet relationship for one part while
    the ``workbook.xml`` sheet id resolves to another. The preflight validates
    the relationship target, so the resolved part must match it before any row
    is read.
    """
    path = write_rows_workbook(tmp_path, [excel_row()])
    entries = archive_entries(path)
    second_sheet = entries["xl/worksheets/sheet1.xml"]
    entries["xl/worksheets/sheet2.xml"] = second_sheet
    rels = entries["xl/_rels/workbook.xml.rels"]
    worksheet_relationship = (
        b'<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
        b'2006/relationships/worksheet" Target="/xl/worksheets/sheet1.xml" '
        b'Id="rId1" />'
    )
    assert worksheet_relationship in rels
    entries["xl/_rels/workbook.xml.rels"] = rels.replace(
        worksheet_relationship,
        b'<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
        b'2006/relationships/styles" Target="/xl/worksheets/sheet2.xml" '
        b'Id="rId1" /><Relationship Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/'
        b'sheet1.xml" Id="rId2" />',
        1,
    )
    repack_entries(path, entries)

    assert_invalid_excel(path, "Import Excel file is invalid")
