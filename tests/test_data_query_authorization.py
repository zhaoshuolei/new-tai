"""Customer row-scope and deleted-customer authorization tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataRecord,
    DataSnapshotStatus,
    DataTableDefinition,
)
from app.models.department import Department
from app.models.role import DataScope
from app.models.user import User
from app.schemas.data_query import DataRecordSearch
from app.services import data_query as data_query_service
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_customer_row,
    make_department,
    make_user,
)
from tests.data_query_helpers import (
    context_for,
    create_child_department,
    create_import_job,
    create_query_table,
    create_record,
    create_snapshot,
    mark_customer_deleted,
)


@dataclass(frozen=True)
class AuthorizationData:
    table: DataTableDefinition
    empty_table: DataTableDefinition
    seller: User
    main_department: Department
    child_department: Department
    marketing_department: Department
    seller_customer: Customer
    peer_customer: Customer
    child_customer: Customer
    hidden_customer: Customer
    records: dict[str, DataRecord]


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
async def authorization_data(
    session: AsyncSession,
) -> AuthorizationData:
    main_department = await make_department(session)
    child_department = await create_child_department(
        session,
        parent=main_department,
    )
    marketing_department = await make_department(session)

    seller = await make_user(session, department=main_department)
    peer = await make_user(session, department=main_department)
    child = await make_user(session, department=child_department)
    outsider = await make_user(session, department=marketing_department)

    seller_customer = await make_customer_row(
        session,
        owner=seller,
        name=f"seller-{uuid4()}",
    )
    peer_customer = await make_customer_row(
        session,
        owner=peer,
        name=f"peer-{uuid4()}",
    )
    child_customer = await make_customer_row(
        session,
        owner=child,
        name=f"child-{uuid4()}",
    )
    hidden_customer = await make_customer_row(
        session,
        owner=outsider,
        name=f"outsider-{uuid4()}",
    )

    table, _fields = await create_query_table(session, seller)
    empty_table, _empty_fields = await create_query_table(session, seller)
    job = await create_import_job(session, table=table, owner=seller)
    first_snapshot = await create_snapshot(
        session,
        table=table,
        job=job,
        business_date=date(2026, 10, 1),
        version=1,
        current=True,
        status=DataSnapshotStatus.ACTIVE,
    )
    second_snapshot = await create_snapshot(
        session,
        table=table,
        job=job,
        business_date=date(2026, 10, 2),
        version=1,
        current=True,
        status=DataSnapshotStatus.ACTIVE,
    )

    records = {
        "upstream_visible": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=seller_customer,
            downstream=hidden_customer,
            revenue=Decimal("10"),
            cost=Decimal("4"),
        ),
        "downstream_visible": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=hidden_customer,
            downstream=seller_customer,
            revenue=Decimal("20"),
            cost=Decimal("8"),
        ),
        "hidden": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=hidden_customer,
            downstream=hidden_customer,
            revenue=Decimal("30"),
            cost=Decimal("12"),
        ),
        "department_peer": await create_record(
            session,
            snapshot=second_snapshot,
            upstream=peer_customer,
            downstream=peer_customer,
            revenue=Decimal("40"),
            cost=Decimal("16"),
        ),
        "child_department": await create_record(
            session,
            snapshot=second_snapshot,
            upstream=child_customer,
            downstream=child_customer,
            revenue=Decimal("50"),
            cost=Decimal("20"),
        ),
    }
    return AuthorizationData(
        table=table,
        empty_table=empty_table,
        seller=seller,
        main_department=main_department,
        child_department=child_department,
        marketing_department=marketing_department,
        seller_customer=seller_customer,
        peer_customer=peer_customer,
        child_customer=child_customer,
        hidden_customer=hidden_customer,
        records=records,
    )


def _search() -> DataRecordSearch:
    return DataRecordSearch(page=1, page_size=100)


def _context(
    data: AuthorizationData,
    *,
    data_scope: DataScope | None,
    role_id: int | None = 10,
    is_super_admin: bool = False,
    custom_department_ids: frozenset[int] = frozenset(),
    include_children: bool = False,
) -> AuthContext:
    return context_for(
        data.seller,
        data_scope=data_scope,
        role_id=role_id,
        is_super_admin=is_super_admin,
        custom_department_ids=custom_department_ids,
        include_children=include_children,
    )


async def test_visible_subquery_matches_each_existing_scope(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    contexts = {
        "no_role": _context(data, data_scope=None, role_id=None),
        "self": _context(data, data_scope=DataScope.SELF),
        "department": _context(data, data_scope=DataScope.DEPT),
        "department_and_children": _context(
            data,
            data_scope=DataScope.DEPT_AND_CHILD,
        ),
        "custom_exact": _context(
            data,
            data_scope=DataScope.CUSTOM,
            custom_department_ids=frozenset({data.main_department.id}),
        ),
        "custom_children": _context(
            data,
            data_scope=DataScope.CUSTOM,
            custom_department_ids=frozenset({data.main_department.id}),
            include_children=True,
        ),
        "all": _context(data, data_scope=DataScope.ALL),
        "roleless_all": _context(
            data,
            data_scope=DataScope.ALL,
            role_id=None,
        ),
        "super_without_role": _context(
            data,
            data_scope=None,
            role_id=None,
            is_super_admin=True,
        ),
    }
    seller_id = data.seller_customer.id
    peer_id = data.peer_customer.id
    child_id = data.child_customer.id
    hidden_id = data.hidden_customer.id
    expected = {
        "no_role": {seller_id},
        "self": {seller_id},
        "department": {seller_id, peer_id},
        "department_and_children": {seller_id, peer_id, child_id},
        "custom_exact": {seller_id, peer_id},
        "custom_children": {seller_id, peer_id, child_id},
        "all": {seller_id, peer_id, child_id, hidden_id},
        "roleless_all": {seller_id},
        "super_without_role": {seller_id, peer_id, child_id, hidden_id},
    }

    for label, context in contexts.items():
        actual = set(
            (
                await session.scalars(
                    data_query_service.visible_customer_id_subquery(context)
                )
            ).all()
        )
        assert actual == expected[label], label


async def test_either_visible_customer_returns_the_full_row(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    result = await data_query_service.search_records(
        session,
        data.table.id,
        _search(),
        context=_context(data, data_scope=DataScope.SELF),
    )

    upstream = next(
        item for item in result.items if item.id == data.records["upstream_visible"].id
    )
    downstream = next(
        item
        for item in result.items
        if item.id == data.records["downstream_visible"].id
    )
    assert upstream.upstream_customer_id == data.seller_customer.id
    assert upstream.downstream_customer_id == data.hidden_customer.id
    assert upstream.downstream_customer_name == data.hidden_customer.name
    assert upstream.revenue == Decimal("10.00000000")
    assert upstream.cost == Decimal("4.00000000")
    assert downstream.downstream_customer_id == data.seller_customer.id
    assert downstream.upstream_customer_id == data.hidden_customer.id
    assert downstream.upstream_customer_name == data.hidden_customer.name


async def test_scope_filters_count_summary_and_items_before_pagination(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    result = await data_query_service.search_records(
        session,
        data.table.id,
        DataRecordSearch(page=1, page_size=1),
        context=_context(data, data_scope=DataScope.SELF),
    )

    assert len(result.items) == 1
    assert result.total == 2
    assert result.summary.row_count == 2
    assert result.summary.revenue == Decimal("30.00000000")
    assert result.summary.cost == Decimal("12.00000000")
    assert result.summary.gross_profit == Decimal("18.00000000")
    assert {
        data.records["upstream_visible"].id,
        data.records["downstream_visible"].id,
    } == {
        item.id
        for item in (
            await data_query_service.search_records(
                session,
                data.table.id,
                _search(),
                context=_context(data, data_scope=DataScope.SELF),
            )
        ).items
    }


async def test_department_scope_uses_customer_owner_department(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    result = await data_query_service.search_records(
        session,
        data.table.id,
        _search(),
        context=_context(data, data_scope=DataScope.DEPT),
    )

    assert {item.id for item in result.items} == {
        data.records["upstream_visible"].id,
        data.records["downstream_visible"].id,
        data.records["department_peer"].id,
    }
    assert result.total == 3
    assert result.summary.revenue == Decimal("70.00000000")


async def test_all_scope_and_super_admin_see_every_current_row(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    all_result = await data_query_service.search_records(
        session,
        data.table.id,
        _search(),
        context=_context(data, data_scope=DataScope.ALL),
    )
    super_result = await data_query_service.search_records(
        session,
        data.table.id,
        _search(),
        context=_context(
            data,
            data_scope=None,
            role_id=None,
            is_super_admin=True,
        ),
    )

    expected = set(data.records[key].id for key in data.records)
    assert {item.id for item in all_result.items} == expected
    assert {item.id for item in super_result.items} == expected
    assert all_result.total == super_result.total == 5


async def test_roleless_all_uses_self_scope_for_search_and_table_visibility(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    context = _context(
        data,
        data_scope=DataScope.ALL,
        role_id=None,
        is_super_admin=False,
    )
    visible_ids = {
        data.records["upstream_visible"].id,
        data.records["downstream_visible"].id,
    }

    result = await data_query_service.search_records(
        session,
        data.table.id,
        DataRecordSearch(page=1, page_size=1),
        context=context,
    )
    summaries = await data_query_service.list_table_visibility_summaries(
        session,
        [data.table.id],
        context=context,
    )

    assert len(result.items) == 1
    assert result.items[0].id in visible_ids
    assert result.total == 2
    assert result.summary.row_count == 2
    assert result.summary.revenue == Decimal("30.00000000")
    assert result.summary.cost == Decimal("12.00000000")
    assert summaries[data.table.id].row_count == 2
    assert summaries[data.table.id].date_from == date(2026, 10, 1)
    assert summaries[data.table.id].date_to == date(2026, 10, 1)


async def test_soft_deleted_customer_remains_owned_and_never_rebinds_by_name(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    original_id = data.seller_customer.id
    original_name = data.seller_customer.name
    await mark_customer_deleted(session, data.seller_customer)
    reused = await make_customer_row(
        session,
        owner=data.seller,
        name=original_name,
    )

    result = await data_query_service.search_records(
        session,
        data.table.id,
        _search(),
        context=_context(data, data_scope=DataScope.SELF),
    )

    assert {item.id for item in result.items} == {
        data.records["upstream_visible"].id,
        data.records["downstream_visible"].id,
    }
    upstream = next(
        item for item in result.items if item.id == data.records["upstream_visible"].id
    )
    assert upstream.upstream_customer_id == original_id
    assert upstream.upstream_customer_id != reused.id
    assert upstream.upstream_customer_name == original_name
    assert upstream.upstream_customer_name_source == original_name
    assert upstream.upstream_customer_deleted is True


async def test_renamed_customer_returns_current_name_and_source_snapshot(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    source_name = data.seller_customer.name
    current_name = f"renamed-{uuid4()}"
    data.seller_customer.name = current_name
    await session.flush()

    result = await data_query_service.search_records(
        session,
        data.table.id,
        DataRecordSearch(
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 1),
        ),
        context=_context(data, data_scope=DataScope.SELF),
    )

    upstream = next(
        item for item in result.items if item.id == data.records["upstream_visible"].id
    )
    assert upstream.upstream_customer_name == current_name
    assert upstream.upstream_customer_name_source == source_name


async def test_table_visibility_summaries_include_zero_visible_tables(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    missing_id = 999_999_999
    summaries = await data_query_service.list_table_visibility_summaries(
        session,
        [data.table.id, data.empty_table.id, missing_id],
        context=_context(data, data_scope=DataScope.SELF),
    )

    assert set(summaries) == {data.table.id, data.empty_table.id, missing_id}
    assert summaries[data.table.id].row_count == 2
    assert summaries[data.table.id].date_from == date(2026, 10, 1)
    assert summaries[data.table.id].date_to == date(2026, 10, 1)
    assert summaries[data.empty_table.id].row_count == 0
    assert summaries[data.empty_table.id].date_from is None
    assert summaries[data.empty_table.id].date_to is None
    assert summaries[missing_id].row_count == 0
    assert summaries[missing_id].date_from is None
    assert summaries[missing_id].date_to is None


async def test_cross_date_current_pointer_is_ignored_by_visibility_summary(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    malformed_record = data.records["department_peer"]
    await session.execute(
        update(DataRecord)
        .where(DataRecord.id == malformed_record.id)
        .values(business_date=date(2026, 10, 1))
    )
    await session.execute(
        update(DataCurrentSnapshot)
        .where(
            DataCurrentSnapshot.c.table_id == data.table.id,
            DataCurrentSnapshot.c.business_date == date(2026, 10, 1),
        )
        .values(snapshot_id=malformed_record.snapshot_id)
    )

    summaries = await data_query_service.list_table_visibility_summaries(
        session,
        [data.table.id],
        context=_context(data, data_scope=DataScope.ALL),
    )

    assert summaries[data.table.id].row_count == 1
    assert summaries[data.table.id].date_from == date(2026, 10, 2)
    assert summaries[data.table.id].date_to == date(2026, 10, 2)


async def test_all_scope_summary_has_no_customer_scope_requirement(
    session: AsyncSession,
    authorization_data: AuthorizationData,
) -> None:
    data = authorization_data
    summaries = await data_query_service.list_table_visibility_summaries(
        session,
        [data.table.id],
        context=_context(data, data_scope=DataScope.ALL),
    )

    assert summaries[data.table.id].row_count == 5
    assert summaries[data.table.id].date_from == date(2026, 10, 1)
    assert summaries[data.table.id].date_to == date(2026, 10, 2)
