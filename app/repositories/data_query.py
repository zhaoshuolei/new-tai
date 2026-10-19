"""Current-snapshot query construction and persistence access.

The current pointer is the only source of "current" rows. The version columns
on records and snapshots are never consulted for selection.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
)
from app.repositories.data_table import ResolvedDataDefinition

type QueryValue = str | int | Decimal | date | bool


@dataclass(frozen=True)
class QueryCondition:
    """A service-validated condition tied to an owned field definition."""

    field: DataFieldDefinition
    operator: str
    value: object


@dataclass(frozen=True)
class QueryRecordRow:
    id: int
    business_date: date
    upstream_customer_id: int
    downstream_customer_id: int
    upstream_customer_name: str
    downstream_customer_name: str
    upstream_customer_deleted: bool
    downstream_customer_deleted: bool
    upstream_customer_name_source: str
    downstream_customer_name_source: str
    revenue: Decimal
    cost: Decimal
    custom_values: Mapping[int, QueryValue]


@dataclass(frozen=True)
class CurrentRecordPage:
    items: tuple[QueryRecordRow, ...]
    total: int
    revenue: Decimal
    cost: Decimal


@dataclass(frozen=True)
class TableVisibilityAggregate:
    row_count: int
    date_from: date | None
    date_to: date | None


def _base_record_select(
    columns: Sequence[Any],
    *,
    table_ids: Sequence[int],
) -> Select[Any]:
    return (
        select(*columns)
        .select_from(DataRecord)
        .join(
            DataCurrentSnapshot,
            and_(
                DataCurrentSnapshot.c.table_id == DataRecord.table_id,
                DataCurrentSnapshot.c.business_date == DataRecord.business_date,
                DataCurrentSnapshot.c.snapshot_id == DataRecord.snapshot_id,
            ),
        )
        .join(
            DataSnapshot,
            and_(
                DataSnapshot.id == DataCurrentSnapshot.c.snapshot_id,
                DataSnapshot.table_id == DataCurrentSnapshot.c.table_id,
            ),
        )
        .where(
            DataRecord.table_id.in_(table_ids),
            DataCurrentSnapshot.c.table_id.in_(table_ids),
            DataSnapshot.table_id.in_(table_ids),
            DataSnapshot.status == DataSnapshotStatus.ACTIVE,
        )
    )


def _fixed_field_column(field: DataFieldDefinition) -> Any:
    columns = {
        DataFieldRole.BUSINESS_DATE: DataRecord.business_date,
        DataFieldRole.UPSTREAM_CUSTOMER: DataRecord.upstream_customer_id,
        DataFieldRole.DOWNSTREAM_CUSTOMER: DataRecord.downstream_customer_id,
        DataFieldRole.REVENUE: DataRecord.revenue,
        DataFieldRole.COST: DataRecord.cost,
    }
    return columns[field.system_key]


def _custom_value_column(
    field: DataFieldDefinition,
    aliases: Mapping[int, Any],
) -> Any:
    alias = aliases[field.id]
    columns = {
        DataFieldType.TEXT: alias.text_value,
        DataFieldType.INTEGER: alias.integer_value,
        DataFieldType.DECIMAL: alias.decimal_value,
        DataFieldType.DATE: alias.date_value,
        DataFieldType.BOOLEAN: alias.boolean_value,
        DataFieldType.ENUM: alias.enum_option_id,
    }
    return columns[field.field_type]


def _field_value_column(
    field: DataFieldDefinition,
    aliases: Mapping[int, Any],
) -> Any:
    if field.system_key == DataFieldRole.CUSTOM:
        return _custom_value_column(field, aliases)
    return _fixed_field_column(field)


def _escaped_contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _condition_expression(
    condition: QueryCondition,
    aliases: Mapping[int, Any],
) -> Any:
    column = _field_value_column(condition.field, aliases)
    if condition.operator == "eq":
        return column == condition.value
    if condition.operator == "contains":
        return column.like(
            _escaped_contains_pattern(str(condition.value)),
            escape="\\",
        )
    if condition.operator == "gte":
        return column >= condition.value
    if condition.operator == "lte":
        return column <= condition.value
    if condition.operator == "in":
        return column.in_(condition.value)
    raise ValueError(f"Unsupported normalized operator: {condition.operator}")


def _join_custom_fields(
    statement: Select[Any],
    *,
    conditions: Sequence[QueryCondition],
    sort_field: DataFieldDefinition | None,
) -> tuple[Select[Any], dict[int, Any]]:
    fields: dict[int, DataFieldDefinition] = {
        condition.field.id: condition.field
        for condition in conditions
        if condition.field.system_key == DataFieldRole.CUSTOM
    }
    if sort_field is not None and sort_field.system_key == DataFieldRole.CUSTOM:
        fields[sort_field.id] = sort_field

    aliases: dict[int, Any] = {}
    for field_id in sorted(fields):
        alias = aliased(DataRecordValue)
        aliases[field_id] = alias
        statement = statement.join(
            alias,
            and_(
                alias.record_id == DataRecord.id,
                alias.field_id == field_id,
            ),
        )
    return statement, aliases


def _apply_customer_scope(
    statement: Any,
    *,
    scope_customer_ids: Select[int] | None,
) -> Any:
    if scope_customer_ids is None:
        return statement
    visible = scope_customer_ids.cte("visible_customer_ids")
    return statement.where(
        or_(
            DataRecord.upstream_customer_id.in_(select(visible.c.id)),
            DataRecord.downstream_customer_id.in_(select(visible.c.id)),
        )
    )


def _apply_record_conditions(
    statement: Select[Any],
    *,
    conditions: Sequence[QueryCondition],
    sort_field: DataFieldDefinition | None,
    date_from: date | None,
    date_to: date | None,
    upstream_customer_ids: Sequence[int],
    downstream_customer_ids: Sequence[int],
    scope_customer_ids: Select[int] | None,
) -> tuple[Select[Any], dict[int, Any]]:
    statement, aliases = _join_custom_fields(
        statement,
        conditions=conditions,
        sort_field=sort_field,
    )
    predicates: list[Any] = []
    if date_from is not None:
        predicates.append(DataRecord.business_date >= date_from)
    if date_to is not None:
        predicates.append(DataRecord.business_date <= date_to)
    if upstream_customer_ids:
        predicates.append(
            DataRecord.upstream_customer_id.in_(list(upstream_customer_ids))
        )
    if downstream_customer_ids:
        predicates.append(
            DataRecord.downstream_customer_id.in_(list(downstream_customer_ids))
        )
    predicates.extend(
        _condition_expression(condition, aliases) for condition in conditions
    )
    if predicates:
        statement = statement.where(and_(*predicates))
    statement = _apply_customer_scope(
        statement,
        scope_customer_ids=scope_customer_ids,
    )
    return statement, aliases


async def _aggregate_current_records(
    session: AsyncSession,
    *,
    table_id: int,
    conditions: Sequence[QueryCondition],
    date_from: date | None,
    date_to: date | None,
    upstream_customer_ids: Sequence[int],
    downstream_customer_ids: Sequence[int],
    scope_customer_ids: Select[int] | None,
) -> tuple[int, Decimal, Decimal]:
    statement = _base_record_select(
        (
            DataRecord.id.label("record_id"),
            DataRecord.revenue.label("revenue"),
            DataRecord.cost.label("cost"),
        ),
        table_ids=(table_id,),
    )
    statement, _aliases = _apply_record_conditions(
        statement,
        conditions=conditions,
        sort_field=None,
        date_from=date_from,
        date_to=date_to,
        upstream_customer_ids=upstream_customer_ids,
        downstream_customer_ids=downstream_customer_ids,
        scope_customer_ids=scope_customer_ids,
    )
    filtered = statement.order_by(None).subquery("filtered_current_records")
    aggregate_statement = select(
        func.count(filtered.c.record_id).label("row_count"),
        func.coalesce(func.sum(filtered.c.revenue), Decimal("0")).label("revenue"),
        func.coalesce(func.sum(filtered.c.cost), Decimal("0")).label("cost"),
    )
    row = (await session.execute(aggregate_statement)).one()
    return int(row.row_count or 0), Decimal(row.revenue), Decimal(row.cost)


def _sort_column(
    sort_field: DataFieldDefinition | None,
    aliases: Mapping[int, Any],
    *,
    upstream_customer: Any,
    downstream_customer: Any,
) -> Any:
    if sort_field is None:
        return DataRecord.id
    if sort_field.system_key == DataFieldRole.UPSTREAM_CUSTOMER:
        return upstream_customer.name
    if sort_field.system_key == DataFieldRole.DOWNSTREAM_CUSTOMER:
        return downstream_customer.name
    return _field_value_column(sort_field, aliases)


async def _load_page_custom_values(
    session: AsyncSession,
    *,
    record_ids: Sequence[int],
    definition: ResolvedDataDefinition,
) -> dict[int, dict[int, QueryValue]]:
    custom_fields = {field.id: field for field in definition.custom_fields}
    if not record_ids or not custom_fields:
        return {record_id: {} for record_id in record_ids}

    statement = (
        select(
            DataRecordValue.record_id,
            DataRecordValue.field_id,
            DataRecordValue.text_value,
            DataRecordValue.integer_value,
            DataRecordValue.decimal_value,
            DataRecordValue.date_value,
            DataRecordValue.boolean_value,
            DataRecordValue.enum_option_id,
            DataEnumOption.label.label("enum_label"),
        )
        .outerjoin(
            DataEnumOption,
            DataEnumOption.id == DataRecordValue.enum_option_id,
        )
        .where(
            DataRecordValue.record_id.in_(list(record_ids)),
            DataRecordValue.field_id.in_(list(custom_fields)),
        )
        .order_by(DataRecordValue.record_id, DataRecordValue.field_id)
    )
    values: dict[int, dict[int, QueryValue]] = {
        record_id: {} for record_id in record_ids
    }
    for row in (await session.execute(statement)).all():
        field = custom_fields.get(row.field_id)
        if field is None:
            continue
        value: QueryValue | None
        if field.field_type == DataFieldType.TEXT:
            value = row.text_value
        elif field.field_type == DataFieldType.INTEGER:
            value = row.integer_value
        elif field.field_type == DataFieldType.DECIMAL:
            value = row.decimal_value
        elif field.field_type == DataFieldType.DATE:
            value = row.date_value
        elif field.field_type == DataFieldType.BOOLEAN:
            value = row.boolean_value
        else:
            value = row.enum_label
        if value is not None:
            values[row.record_id][row.field_id] = value
    return values


async def search_current_records(
    session: AsyncSession,
    *,
    definition: ResolvedDataDefinition,
    conditions: Sequence[QueryCondition],
    sort_field: DataFieldDefinition | None,
    sort_direction: str,
    page: int,
    page_size: int,
    date_from: date | None,
    date_to: date | None,
    upstream_customer_ids: Sequence[int],
    downstream_customer_ids: Sequence[int],
    scope_customer_ids: Select[int] | None,
) -> CurrentRecordPage:
    table_id = definition.table.id
    total, revenue, cost = await _aggregate_current_records(
        session,
        table_id=table_id,
        conditions=conditions,
        date_from=date_from,
        date_to=date_to,
        upstream_customer_ids=upstream_customer_ids,
        downstream_customer_ids=downstream_customer_ids,
        scope_customer_ids=scope_customer_ids,
    )
    if total == 0:
        return CurrentRecordPage(
            items=(),
            total=0,
            revenue=Decimal("0"),
            cost=Decimal("0"),
        )

    upstream_customer = aliased(Customer)
    downstream_customer = aliased(Customer)
    statement = _base_record_select(
        (
            DataRecord.id.label("record_id"),
            DataRecord.business_date,
            DataRecord.upstream_customer_id,
            DataRecord.downstream_customer_id,
            upstream_customer.name.label("upstream_customer_name"),
            downstream_customer.name.label("downstream_customer_name"),
            upstream_customer.status.label("upstream_customer_status"),
            downstream_customer.status.label("downstream_customer_status"),
            DataRecord.upstream_customer_name_source,
            DataRecord.downstream_customer_name_source,
            DataRecord.revenue,
            DataRecord.cost,
        ),
        table_ids=(table_id,),
    )
    statement, aliases = _apply_record_conditions(
        statement,
        conditions=conditions,
        sort_field=sort_field,
        date_from=date_from,
        date_to=date_to,
        upstream_customer_ids=upstream_customer_ids,
        downstream_customer_ids=downstream_customer_ids,
        scope_customer_ids=scope_customer_ids,
    )
    statement = statement.join(
        upstream_customer,
        upstream_customer.id == DataRecord.upstream_customer_id,
    ).join(
        downstream_customer,
        downstream_customer.id == DataRecord.downstream_customer_id,
    )

    sort_column = _sort_column(
        sort_field,
        aliases,
        upstream_customer=upstream_customer,
        downstream_customer=downstream_customer,
    )
    direction = sort_column.asc() if sort_direction == "asc" else sort_column.desc()
    statement = (
        statement.order_by(direction, DataRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(statement)).all()
    record_ids = [row.record_id for row in rows]
    values_by_record = await _load_page_custom_values(
        session,
        record_ids=record_ids,
        definition=definition,
    )
    items = tuple(
        QueryRecordRow(
            id=row.record_id,
            business_date=row.business_date,
            upstream_customer_id=row.upstream_customer_id,
            downstream_customer_id=row.downstream_customer_id,
            upstream_customer_name=row.upstream_customer_name,
            downstream_customer_name=row.downstream_customer_name,
            upstream_customer_deleted=row.upstream_customer_status
            == ContactStatus.DELETED,
            downstream_customer_deleted=row.downstream_customer_status
            == ContactStatus.DELETED,
            upstream_customer_name_source=row.upstream_customer_name_source,
            downstream_customer_name_source=row.downstream_customer_name_source,
            revenue=Decimal(row.revenue),
            cost=Decimal(row.cost),
            custom_values=values_by_record.get(row.record_id, {}),
        )
        for row in rows
    )
    return CurrentRecordPage(
        items=items,
        total=total,
        revenue=revenue,
        cost=cost,
    )


async def aggregate_table_visibility(
    session: AsyncSession,
    *,
    table_ids: Sequence[int],
    scope_customer_ids: Select[int] | None,
) -> dict[int, TableVisibilityAggregate]:
    if not table_ids:
        return {}
    statement = (
        select(
            DataRecord.table_id,
            func.count(DataRecord.id).label("row_count"),
            func.min(DataRecord.business_date).label("date_from"),
            func.max(DataRecord.business_date).label("date_to"),
        )
        .select_from(DataRecord)
        .join(
            DataCurrentSnapshot,
            and_(
                DataCurrentSnapshot.c.table_id == DataRecord.table_id,
                DataCurrentSnapshot.c.business_date == DataRecord.business_date,
                DataCurrentSnapshot.c.snapshot_id == DataRecord.snapshot_id,
            ),
        )
        .join(
            DataSnapshot,
            and_(
                DataSnapshot.id == DataCurrentSnapshot.c.snapshot_id,
                DataSnapshot.table_id == DataCurrentSnapshot.c.table_id,
            ),
        )
        .where(
            DataRecord.table_id.in_(list(table_ids)),
            DataCurrentSnapshot.c.table_id.in_(list(table_ids)),
            DataSnapshot.table_id.in_(list(table_ids)),
            DataSnapshot.status == DataSnapshotStatus.ACTIVE,
        )
    )
    statement = _apply_customer_scope(
        statement,
        scope_customer_ids=scope_customer_ids,
    ).group_by(DataRecord.table_id)
    aggregates: dict[int, TableVisibilityAggregate] = {}
    for row in (await session.execute(statement)).all():
        aggregates[row.table_id] = TableVisibilityAggregate(
            row_count=int(row.row_count),
            date_from=row.date_from,
            date_to=row.date_to,
        )
    return aggregates


__all__: tuple[str, ...] = (
    "CurrentRecordPage",
    "QueryCondition",
    "QueryRecordRow",
    "TableVisibilityAggregate",
    "aggregate_table_visibility",
    "search_current_records",
)
