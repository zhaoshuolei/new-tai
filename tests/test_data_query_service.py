"""Current-snapshot query, filtering, sorting, paging, and summary tests."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import DataCurrentSnapshot
from app.models.role import DataScope
from app.schemas.data_query import DataFieldCondition, DataRecordSearch
from app.services import data_query as data_query_service
from tests.customer_helpers import TEST_DATABASE_URL
from tests.data_query_helpers import (
    QueryDataset,
    context_for,
    seed_query_dataset,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def dataset(session: AsyncSession) -> QueryDataset:
    return await seed_query_dataset(session)


def _all_search(**overrides: Any) -> DataRecordSearch:
    values: dict[str, Any] = {"page": 1, "page_size": 20}
    values.update(overrides)
    return DataRecordSearch(**values)


async def test_search_defaults_are_independent_and_contract_values(
    dataset: QueryDataset,
) -> None:
    default = DataRecordSearch()

    assert default.date_from is None
    assert default.date_to is None
    assert default.upstream_customer_ids == []
    assert default.downstream_customer_ids == []
    assert default.conditions == []
    assert default.sort_field_id is None
    assert default.sort_direction == "desc"
    assert default.page == 1
    assert default.page_size == 20

    default.upstream_customer_ids.append(dataset.visible_customer.id)
    default.downstream_customer_ids.append(dataset.hidden_customer.id)
    default.conditions.append(
        DataFieldCondition(field_id=dataset.fields.text.id, operator="eq", value="x")
    )
    fresh = DataRecordSearch()
    assert fresh.upstream_customer_ids == []
    assert fresh.downstream_customer_ids == []
    assert fresh.conditions == []


async def test_query_uses_current_pointers_and_ignores_higher_superseded_version(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(),
        context=context_for(dataset.owner, data_scope=DataScope.ALL),
    )

    current_ids = [item.id for item in result.items]
    assert current_ids == [
        dataset.records["zero_revenue"].id,
        dataset.records["hidden"].id,
        dataset.records["second"].id,
        dataset.records["first"].id,
    ]
    assert dataset.superseded_record.id not in current_ids


async def test_pointer_to_superseded_snapshot_never_exposes_its_rows(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    await session.execute(
        update(DataCurrentSnapshot)
        .where(
            DataCurrentSnapshot.c.table_id == dataset.table.id,
            DataCurrentSnapshot.c.business_date == date(2026, 10, 2),
        )
        .values(snapshot_id=dataset.superseded_record.snapshot_id)
    )

    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            date_from=date(2026, 10, 2),
            date_to=date(2026, 10, 2),
        ),
        context=context_for(dataset.owner),
    )

    assert result.items == []
    assert result.total == 0


async def test_default_page_returns_complete_typed_row(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(page_size=2),
        context=context_for(dataset.owner),
    )

    first_line = result.items[1]
    assert first_line.id == dataset.records["hidden"].id
    assert first_line.business_date == date(2026, 10, 1)
    assert first_line.upstream_customer_id == dataset.hidden_customer.id
    assert first_line.downstream_customer_id == dataset.hidden_customer.id
    assert first_line.upstream_customer_name == dataset.hidden_customer.name
    assert first_line.downstream_customer_name == dataset.hidden_customer.name
    assert first_line.upstream_customer_deleted is False
    assert first_line.downstream_customer_deleted is False
    assert first_line.upstream_customer_name_source == dataset.hidden_customer.name
    assert first_line.downstream_customer_name_source == dataset.hidden_customer.name
    assert first_line.revenue == Decimal("-10.00000000")
    assert first_line.cost == Decimal("5.00000000")
    assert first_line.gross_profit == Decimal("-15.00000000")
    assert first_line.gross_margin == Decimal("1.5")
    assert first_line.custom_values == {
        dataset.fields.text.id: "100XXbeta",
        dataset.fields.integer.id: 30,
        dataset.fields.decimal.id: Decimal("3.50000000"),
        dataset.fields.custom_date.id: date(2026, 1, 3),
        dataset.fields.boolean.id: True,
        dataset.fields.enum.id: "Low",
    }
    assert all(
        not isinstance(value, float) for value in first_line.custom_values.values()
    )


async def test_gross_margin_is_none_when_revenue_is_zero(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            date_from=date(2026, 10, 2),
            date_to=date(2026, 10, 2),
        ),
        context=context_for(dataset.owner),
    )

    assert len(result.items) == 1
    assert result.items[0].revenue == Decimal("0.00000000")
    assert result.items[0].cost == Decimal("5.00000000")
    assert result.items[0].gross_profit == Decimal("-5.00000000")
    assert result.items[0].gross_margin is None


async def test_summary_covers_full_filtered_set_not_current_page(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(page=1, page_size=1),
        context=context_for(dataset.owner),
    )

    assert len(result.items) == 1
    assert result.total == 4
    assert result.summary.row_count == 4
    assert result.summary.revenue == Decimal("140.00000000")
    assert result.summary.cost == Decimal("110.00000000")
    assert result.summary.gross_profit == Decimal("30.00000000")
    assert result.summary.gross_margin == (
        Decimal("30.00000000") / Decimal("140.00000000")
    )


async def test_empty_result_has_zero_totals_and_no_margin(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(date_from=date(2030, 1, 1)),
        context=context_for(dataset.owner),
    )

    assert result.items == []
    assert result.total == 0
    assert result.summary.row_count == 0
    assert result.summary.revenue == Decimal("0")
    assert result.summary.cost == Decimal("0")
    assert result.summary.gross_profit == Decimal("0")
    assert result.summary.gross_margin is None


async def test_date_and_both_customer_filters_are_and_combined(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 1),
            upstream_customer_ids=[dataset.hidden_customer.id],
            downstream_customer_ids=[dataset.second_visible_customer.id],
        ),
        context=context_for(dataset.owner),
    )

    assert [item.id for item in result.items] == [dataset.records["second"].id]


async def test_operator_and_type_matrix(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    f = dataset.fields
    cases = [
        ("text eq", f.text.id, "eq", "Alpha", {"first"}),
        ("text contains literal wildcards", f.text.id, "contains", "%_", {"second"}),
        ("integer eq", f.integer.id, "eq", 20, {"second"}),
        ("integer gte", f.integer.id, "gte", 30, {"hidden", "zero_revenue"}),
        ("integer lte", f.integer.id, "lte", 10, {"first"}),
        (
            "decimal eq",
            f.decimal.id,
            "eq",
            Decimal("2.5"),
            {"second"},
        ),
        (
            "decimal gte",
            f.decimal.id,
            "gte",
            Decimal("3.5"),
            {"hidden", "zero_revenue"},
        ),
        (
            "decimal lte",
            f.decimal.id,
            "lte",
            Decimal("1.5"),
            {"first"},
        ),
        ("date eq", f.custom_date.id, "eq", date(2026, 1, 2), {"second"}),
        (
            "date gte",
            f.custom_date.id,
            "gte",
            date(2026, 1, 3),
            {"hidden", "zero_revenue"},
        ),
        ("date lte", f.custom_date.id, "lte", date(2026, 1, 1), {"first"}),
        ("boolean eq", f.boolean.id, "eq", True, {"first", "hidden"}),
        ("enum eq", f.enum.id, "eq", f.enum_options["Low"], {"first", "hidden"}),
        (
            "enum in",
            f.enum.id,
            "in",
            [f.enum_options["High"]],
            {"second", "zero_revenue"},
        ),
        (
            "business date eq",
            f.business_date.id,
            "eq",
            date(2026, 10, 1),
            {"first", "second", "hidden"},
        ),
        (
            "business date gte",
            f.business_date.id,
            "gte",
            date(2026, 10, 2),
            {"zero_revenue"},
        ),
        (
            "business date lte",
            f.business_date.id,
            "lte",
            date(2026, 10, 1),
            {"first", "second", "hidden"},
        ),
        (
            "upstream customer eq",
            f.upstream_customer.id,
            "eq",
            dataset.visible_customer.id,
            {"first", "zero_revenue"},
        ),
        (
            "downstream customer eq",
            f.downstream_customer.id,
            "eq",
            dataset.second_visible_customer.id,
            {"second"},
        ),
        ("revenue eq", f.revenue.id, "eq", Decimal("100"), {"first"}),
        (
            "revenue gte",
            f.revenue.id,
            "gte",
            Decimal("50"),
            {"first", "second"},
        ),
        (
            "revenue lte",
            f.revenue.id,
            "lte",
            Decimal("0"),
            {"hidden", "zero_revenue"},
        ),
        ("cost eq", f.cost.id, "eq", Decimal("60"), {"second"}),
        (
            "cost gte",
            f.cost.id,
            "gte",
            Decimal("40"),
            {"first", "second"},
        ),
        (
            "cost lte",
            f.cost.id,
            "lte",
            Decimal("5"),
            {"hidden", "zero_revenue"},
        ),
    ]

    for label, field_id, operator, value, expected_keys in cases:
        result = await data_query_service.search_records(
            session,
            dataset.table.id,
            _all_search(
                conditions=[
                    DataFieldCondition(
                        field_id=field_id,
                        operator=operator,
                        value=value,
                    )
                ]
            ),
            context=context_for(dataset.owner),
        )
        actual_ids = {item.id for item in result.items}
        expected_ids = {dataset.records[key].id for key in expected_keys}
        assert actual_ids == expected_ids, label
        assert result.total == len(expected_ids), label
        assert result.summary.row_count == len(expected_ids), label


async def test_fixed_customer_sort_uses_current_names(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    dataset.visible_customer.name = "Zulu Customer"
    dataset.second_visible_customer.name = "Mike Customer"
    dataset.hidden_customer.name = "Alpha Customer"
    await session.flush()

    result = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            sort_field_id=dataset.fields.upstream_customer.id,
            sort_direction="asc",
        ),
        context=context_for(dataset.owner),
    )

    assert [item.upstream_customer_name for item in result.items] == [
        "Alpha Customer",
        "Alpha Customer",
        "Zulu Customer",
        "Zulu Customer",
    ]
    assert [item.id for item in result.items] == [
        dataset.records["hidden"].id,
        dataset.records["second"].id,
        dataset.records["zero_revenue"].id,
        dataset.records["first"].id,
    ]


async def test_custom_sort_is_stable_and_paginated(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    first_page = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            conditions=[],
            sort_field_id=dataset.fields.boolean.id,
            sort_direction="asc",
            page=1,
            page_size=2,
        ),
        context=context_for(dataset.owner),
    )
    second_page = await data_query_service.search_records(
        session,
        dataset.table.id,
        _all_search(
            conditions=[],
            sort_field_id=dataset.fields.boolean.id,
            sort_direction="asc",
            page=2,
            page_size=2,
        ),
        context=context_for(dataset.owner),
    )

    expected = [
        dataset.records["zero_revenue"].id,
        dataset.records["second"].id,
        dataset.records["hidden"].id,
        dataset.records["first"].id,
    ]
    assert [item.id for item in first_page.items] == expected[:2]
    assert [item.id for item in second_page.items] == expected[2:]
    assert first_page.total == second_page.total == 4


async def test_custom_values_for_page_are_loaded_in_one_query(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    statements: list[str] = []

    def capture(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "custom_data_record_value" in statement.lower():
            statements.append(statement)

    sync_engine = session.get_bind().engine
    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        result = await data_query_service.search_records(
            session,
            dataset.table.id,
            _all_search(),
            context=context_for(dataset.owner),
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)

    assert len(result.items) == 4
    assert len(statements) == 1


@pytest.mark.parametrize(
    ("field_name", "operator", "value", "message_fragment"),
    [
        ("unknown", "eq", "x", "field"),
        ("text", "gte", "x", "operator"),
        ("integer", "contains", 1, "operator"),
        ("integer", "eq", "1", "value"),
        ("boolean", "eq", 1, "value"),
        ("enum", "eq", "Low", "option"),
        ("enum", "in", [], "value"),
    ],
)
async def test_invalid_conditions_raise_query_invalid(
    session: AsyncSession,
    dataset: QueryDataset,
    field_name: str,
    operator: str,
    value: object,
    message_fragment: str,
) -> None:
    field_ids = {
        "text": dataset.fields.text.id,
        "integer": dataset.fields.integer.id,
        "boolean": dataset.fields.boolean.id,
        "enum": dataset.fields.enum.id,
    }
    condition = DataFieldCondition(
        field_id=field_ids.get(field_name, 999_999_999),
        operator=operator,
        value=value,
    )

    with pytest.raises(BusinessError) as raised:
        await data_query_service.search_records(
            session,
            dataset.table.id,
            _all_search(conditions=[condition]),
            context=context_for(dataset.owner),
        )

    assert raised.value.code == ErrorCode.DATA_QUERY_INVALID
    assert message_fragment in raised.value.message.lower()


async def test_sort_field_must_belong_to_table(
    session: AsyncSession,
    dataset: QueryDataset,
) -> None:
    with pytest.raises(BusinessError) as raised:
        await data_query_service.search_records(
            session,
            dataset.table.id,
            _all_search(sort_field_id=999_999_999),
            context=context_for(dataset.owner),
        )

    assert raised.value.code == ErrorCode.DATA_QUERY_INVALID
    assert "sort" in raised.value.message.lower()
