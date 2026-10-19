"""Shared real-database fixtures for current-data query tests."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
)
from app.models.department import Department
from app.models.role import DataScope
from app.models.user import User
from tests.customer_helpers import make_customer_row, make_user


@dataclass(frozen=True)
class EnumOptionValue:
    option_id: int


@dataclass(frozen=True)
class QueryFields:
    business_date: DataFieldDefinition
    upstream_customer: DataFieldDefinition
    downstream_customer: DataFieldDefinition
    revenue: DataFieldDefinition
    cost: DataFieldDefinition
    text: DataFieldDefinition
    integer: DataFieldDefinition
    decimal: DataFieldDefinition
    custom_date: DataFieldDefinition
    boolean: DataFieldDefinition
    enum: DataFieldDefinition
    enum_options: dict[str, int]


@dataclass(frozen=True)
class QueryDataset:
    owner: User
    other_owner: User
    table: DataTableDefinition
    fields: QueryFields
    visible_customer: Customer
    second_visible_customer: Customer
    hidden_customer: Customer
    records: dict[str, DataRecord]
    superseded_record: DataRecord


def context_for(
    user: User,
    *,
    data_scope: DataScope = DataScope.ALL,
    role_id: int | None = 1,
    is_super_admin: bool = False,
    custom_department_ids: frozenset[int] = frozenset(),
    include_children: bool = False,
) -> AuthContext:
    return AuthContext(
        user_id=user.id,
        role_id=role_id,
        department_id=user.department_id,
        data_scope=data_scope,
        custom_department_ids=custom_department_ids,
        include_children=include_children,
        permission_codes=frozenset({"data:read"}),
        is_super_admin=is_super_admin,
        username=user.username,
    )


async def create_query_table(
    session: AsyncSession,
    owner: User,
) -> tuple[DataTableDefinition, QueryFields]:
    table = DataTableDefinition(
        name=f"query-table-{uuid4().hex}",
        created_by=owner.id,
    )
    session.add(table)
    await session.flush()

    fixed_specs = (
        ("Business date", DataFieldType.DATE, DataFieldRole.BUSINESS_DATE),
        (
            "Upstream customer",
            DataFieldType.CUSTOMER,
            DataFieldRole.UPSTREAM_CUSTOMER,
        ),
        (
            "Downstream customer",
            DataFieldType.CUSTOMER,
            DataFieldRole.DOWNSTREAM_CUSTOMER,
        ),
        ("Revenue", DataFieldType.DECIMAL, DataFieldRole.REVENUE),
        ("Cost", DataFieldType.DECIMAL, DataFieldRole.COST),
    )
    fields = [
        DataFieldDefinition(
            table_id=table.id,
            label=label,
            field_type=field_type,
            system_key=role,
            required=True,
            sort_order=index,
        )
        for index, (label, field_type, role) in enumerate(fixed_specs)
    ]
    custom_specs = (
        ("Text", DataFieldType.TEXT),
        ("Integer", DataFieldType.INTEGER),
        ("Decimal", DataFieldType.DECIMAL),
        ("Custom date", DataFieldType.DATE),
        ("Boolean", DataFieldType.BOOLEAN),
        ("Enum", DataFieldType.ENUM),
    )
    custom_fields = [
        DataFieldDefinition(
            table_id=table.id,
            label=label,
            field_type=field_type,
            system_key=DataFieldRole.CUSTOM,
            required=False,
            sort_order=len(fields) + index,
        )
        for index, (label, field_type) in enumerate(custom_specs)
    ]
    session.add_all([*fields, *custom_fields])
    await session.flush()

    enum_options = [
        DataEnumOption(field_id=custom_fields[-1].id, label=label, sort_order=index)
        for index, label in enumerate(("Low", "High"))
    ]
    session.add_all(enum_options)
    await session.flush()

    return table, QueryFields(
        business_date=fields[0],
        upstream_customer=fields[1],
        downstream_customer=fields[2],
        revenue=fields[3],
        cost=fields[4],
        text=custom_fields[0],
        integer=custom_fields[1],
        decimal=custom_fields[2],
        custom_date=custom_fields[3],
        boolean=custom_fields[4],
        enum=custom_fields[5],
        enum_options={option.label: option.id for option in enum_options},
    )


async def create_import_job(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    owner: User,
) -> DataImportJob:
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.API,
        status=DataImportStatus.SUCCEEDED,
        storage_path=f"imports/{uuid4().hex}.json",
        content_sha256=uuid4().hex + uuid4().hex,
        size_bytes=100,
        requested_by=owner.id,
    )
    session.add(job)
    await session.flush()
    return job


async def create_snapshot(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    job: DataImportJob,
    business_date: date,
    version: int,
    status: DataSnapshotStatus,
    current: bool,
) -> DataSnapshot:
    snapshot = DataSnapshot(
        table_id=table.id,
        business_date=business_date,
        import_id=job.id,
        version=version,
        status=status,
    )
    session.add(snapshot)
    await session.flush()
    if current:
        await session.execute(
            DataCurrentSnapshot.insert().values(
                table_id=table.id,
                business_date=business_date,
                snapshot_id=snapshot.id,
            )
        )
    return snapshot


async def create_record(
    session: AsyncSession,
    *,
    snapshot: DataSnapshot,
    upstream: Customer,
    downstream: Customer,
    revenue: Decimal,
    cost: Decimal,
    custom_values: dict[int, Any] | None = None,
    upstream_source_name: str | None = None,
    downstream_source_name: str | None = None,
) -> DataRecord:
    record = DataRecord(
        snapshot_id=snapshot.id,
        table_id=snapshot.table_id,
        business_date=snapshot.business_date,
        upstream_customer_id=upstream.id,
        downstream_customer_id=downstream.id,
        upstream_customer_name_source=upstream_source_name or upstream.name,
        downstream_customer_name_source=downstream_source_name or downstream.name,
        revenue=revenue,
        cost=cost,
    )
    session.add(record)
    await session.flush()
    for field_id, value in (custom_values or {}).items():
        session.add(_record_value(record.id, field_id, value))
    await session.flush()
    return record


def _record_value(record_id: int, field_id: int, value: Any) -> DataRecordValue:
    if isinstance(value, EnumOptionValue):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            enum_option_id=value.option_id,
        )
    if isinstance(value, bool):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            boolean_value=value,
        )
    if isinstance(value, date):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            date_value=value,
        )
    if isinstance(value, Decimal):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            decimal_value=value,
        )
    if isinstance(value, int):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            integer_value=value,
        )
    if isinstance(value, str):
        return DataRecordValue(
            record_id=record_id,
            field_id=field_id,
            text_value=value,
        )
    raise TypeError(f"Unsupported test value type: {type(value)!r}")


async def seed_query_dataset(session: AsyncSession) -> QueryDataset:
    owner = await make_user(session)
    other_owner = await make_user(session)
    visible = await make_customer_row(session, owner=owner, name=f"visible-{uuid4()}")
    second_visible = await make_customer_row(
        session,
        owner=owner,
        name=f"visible-two-{uuid4()}",
    )
    hidden = await make_customer_row(
        session,
        owner=other_owner,
        name=f"hidden-{uuid4()}",
    )
    table, fields = await create_query_table(session, owner)
    job = await create_import_job(session, table=table, owner=owner)

    first_date = date(2026, 10, 1)
    second_date = date(2026, 10, 2)
    first_snapshot = await create_snapshot(
        session,
        table=table,
        job=job,
        business_date=first_date,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
        current=True,
    )
    second_snapshot = await create_snapshot(
        session,
        table=table,
        job=job,
        business_date=second_date,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
        current=True,
    )
    superseded_snapshot = await create_snapshot(
        session,
        table=table,
        job=job,
        business_date=second_date,
        version=99,
        status=DataSnapshotStatus.SUPERSEDED,
        current=False,
    )

    def values(
        *,
        text: str,
        integer: int,
        decimal: Decimal,
        custom_date: date,
        boolean: bool,
        enum_label: str,
    ) -> dict[int, Any]:
        return {
            fields.text.id: text,
            fields.integer.id: integer,
            fields.decimal.id: decimal,
            fields.custom_date.id: custom_date,
            fields.boolean.id: boolean,
            fields.enum.id: EnumOptionValue(fields.enum_options[enum_label]),
        }

    rows = {
        "first": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=visible,
            downstream=hidden,
            revenue=Decimal("100.00000000"),
            cost=Decimal("40.00000000"),
            custom_values=values(
                text="Alpha",
                integer=10,
                decimal=Decimal("1.50000000"),
                custom_date=date(2026, 1, 1),
                boolean=True,
                enum_label="Low",
            ),
        ),
        "second": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=hidden,
            downstream=second_visible,
            revenue=Decimal("50.00000000"),
            cost=Decimal("60.00000000"),
            custom_values=values(
                text="R&D 100%_beta",
                integer=20,
                decimal=Decimal("2.50000000"),
                custom_date=date(2026, 1, 2),
                boolean=False,
                enum_label="High",
            ),
        ),
        "hidden": await create_record(
            session,
            snapshot=first_snapshot,
            upstream=hidden,
            downstream=hidden,
            revenue=Decimal("-10.00000000"),
            cost=Decimal("5.00000000"),
            custom_values=values(
                text="100XXbeta",
                integer=30,
                decimal=Decimal("3.50000000"),
                custom_date=date(2026, 1, 3),
                boolean=True,
                enum_label="Low",
            ),
        ),
        "zero_revenue": await create_record(
            session,
            snapshot=second_snapshot,
            upstream=visible,
            downstream=hidden,
            revenue=Decimal("0.00000000"),
            cost=Decimal("5.00000000"),
            custom_values=values(
                text="beta",
                integer=40,
                decimal=Decimal("4.50000000"),
                custom_date=date(2026, 1, 4),
                boolean=False,
                enum_label="High",
            ),
        ),
    }
    superseded = await create_record(
        session,
        snapshot=superseded_snapshot,
        upstream=visible,
        downstream=hidden,
        revenue=Decimal("999.00000000"),
        cost=Decimal("1.00000000"),
    )
    return QueryDataset(
        owner=owner,
        other_owner=other_owner,
        table=table,
        fields=fields,
        visible_customer=visible,
        second_visible_customer=second_visible,
        hidden_customer=hidden,
        records=rows,
        superseded_record=superseded,
    )


async def mark_customer_deleted(session: AsyncSession, customer: Customer) -> None:
    customer.status = ContactStatus.DELETED
    from datetime import UTC, datetime

    customer.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()


async def create_child_department(
    session: AsyncSession,
    *,
    parent: Department,
) -> Department:
    from app.models.department import DepartmentStatus

    child = Department(
        parent_id=parent.id,
        name=f"child-{uuid4().hex}",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    session.add(child)
    await session.flush()
    child.path = f"{parent.path}{child.id}/"
    await session.flush()
    return child


__all__: tuple[str, ...] = (
    "QueryDataset",
    "QueryFields",
    "context_for",
    "create_child_department",
    "create_import_job",
    "create_query_table",
    "create_record",
    "create_snapshot",
    "mark_customer_deleted",
    "seed_query_dataset",
)
