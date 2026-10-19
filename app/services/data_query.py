"""Current-data queries with typed filters and customer row authorization."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.customer import Customer
from app.models.data_management import DataFieldDefinition, DataFieldType
from app.models.role import DataScope
from app.repositories import data_query as data_query_repository
from app.repositories import data_table as data_table_repository
from app.repositories.data_query import QueryCondition
from app.repositories.data_table import ResolvedDataDefinition
from app.schemas.data_query import (
    DataFieldCondition,
    DataRecordLine,
    DataRecordSearch,
    DataSearchResult,
    DataSearchSummary,
    TableVisibilitySummary,
)
from app.services.data_scope import build_customer_scope_filter

_OPERATORS_BY_TYPE: dict[DataFieldType, frozenset[str]] = {
    DataFieldType.TEXT: frozenset({"eq", "contains"}),
    DataFieldType.INTEGER: frozenset({"eq", "gte", "lte"}),
    DataFieldType.DECIMAL: frozenset({"eq", "gte", "lte"}),
    DataFieldType.DATE: frozenset({"eq", "gte", "lte"}),
    DataFieldType.BOOLEAN: frozenset({"eq"}),
    DataFieldType.ENUM: frozenset({"eq", "in"}),
    DataFieldType.CUSTOMER: frozenset({"eq"}),
}


def _invalid_query(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_QUERY_INVALID,
        message,
        http_status=422,
    )


def _table_not_found() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_NOT_FOUND,
        "Data table not found",
        http_status=404,
    )


def _is_unrestricted(context: AuthContext) -> bool:
    return context.is_super_admin or (
        context.role_id is not None and context.data_scope == DataScope.ALL
    )


def visible_customer_id_subquery(context: AuthContext) -> Select[int]:
    """Return customer IDs visible under the existing customer data scope.

    Unrestricted contexts intentionally omit the predicate rather than
    materializing every customer merely to satisfy a row-scope shape.
    """
    statement = select(Customer.id)
    if _is_unrestricted(context):
        return statement
    return statement.where(build_customer_scope_filter(context))


def _normalized_positive_ids(values: Sequence[int], *, name: str) -> tuple[int, ...]:
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values:
        if type(value) is not int or value <= 0:
            raise _invalid_query(f"{name} must contain positive integer IDs")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized)


def _normalize_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise _invalid_query("Decimal filter value has an invalid type")
    if not isinstance(value, (str, int, Decimal)):
        raise _invalid_query("Decimal filter value has an invalid type")
    try:
        normalized = Decimal(value)
    except InvalidOperation, ValueError:
        raise _invalid_query("Decimal filter value is invalid") from None
    if not normalized.is_finite():
        raise _invalid_query("Decimal filter value must be finite")
    return normalized


def _normalize_date(value: object) -> date:
    if isinstance(value, datetime):
        raise _invalid_query("Date filter value has an invalid type")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise _invalid_query("Date filter value has an invalid type")


def _normalize_enum_value(
    value: object,
    *,
    field: DataFieldDefinition,
    definition: ResolvedDataDefinition,
) -> int:
    if type(value) is not int:
        raise _invalid_query("Enum option value must be an option ID")
    option = definition.enum_options_by_id.get(value)
    if option is None or option.field_id != field.id:
        raise _invalid_query("Enum option does not belong to this field")
    return value


def _normalize_condition_value(
    value: object,
    *,
    field: DataFieldDefinition,
    operator: str,
    definition: ResolvedDataDefinition,
) -> object:
    if field.field_type == DataFieldType.TEXT:
        if not isinstance(value, str):
            raise _invalid_query("Text filter value must be a string")
        return value
    if field.field_type == DataFieldType.INTEGER:
        if type(value) is not int:
            raise _invalid_query("Integer filter value must be an integer")
        return value
    if field.field_type == DataFieldType.DECIMAL:
        return _normalize_decimal(value)
    if field.field_type == DataFieldType.DATE:
        return _normalize_date(value)
    if field.field_type == DataFieldType.BOOLEAN:
        if type(value) is not bool:
            raise _invalid_query("Boolean filter value must be true or false")
        return value
    if field.field_type == DataFieldType.CUSTOMER:
        if type(value) is not int or value <= 0:
            raise _invalid_query("Customer filter value must be a positive ID")
        return value
    if field.field_type == DataFieldType.ENUM:
        if operator == "eq":
            return _normalize_enum_value(
                value,
                field=field,
                definition=definition,
            )
        if not isinstance(value, (list, tuple)) or not value:
            raise _invalid_query("Enum in filter value must be a non-empty list")
        return tuple(
            _normalize_enum_value(item, field=field, definition=definition)
            for item in value
        )
    raise _invalid_query("Field type does not support filtering")


def _normalize_condition(
    condition: DataFieldCondition,
    *,
    definition: ResolvedDataDefinition,
) -> QueryCondition:
    field = definition.fields_by_id.get(condition.field_id)
    if field is None:
        raise _invalid_query("Condition field does not belong to this data table")
    operator = condition.operator.strip().lower()
    if operator not in _OPERATORS_BY_TYPE[field.field_type]:
        raise _invalid_query(
            f"Operator {condition.operator!r} is not supported for field {field.id}"
        )
    value = _normalize_condition_value(
        condition.value,
        field=field,
        operator=operator,
        definition=definition,
    )
    return QueryCondition(field=field, operator=operator, value=value)


def _validate_query_shape(query: DataRecordSearch) -> None:
    if query.date_from is not None and query.date_to is not None:
        if query.date_from > query.date_to:
            raise _invalid_query("date_from must not be after date_to")
    if type(query.page) is not int or query.page < 1:
        raise _invalid_query("page must be at least 1")
    if type(query.page_size) is not int or not 1 <= query.page_size <= 100:
        raise _invalid_query("page_size must be between 1 and 100")
    if query.sort_direction not in {"asc", "desc"}:
        raise _invalid_query("sort_direction must be asc or desc")


def _line_from_repository(
    row: data_query_repository.QueryRecordRow,
) -> DataRecordLine:
    gross_profit = row.revenue - row.cost
    gross_margin = None if row.revenue == 0 else gross_profit / row.revenue
    return DataRecordLine(
        id=row.id,
        business_date=row.business_date,
        upstream_customer_id=row.upstream_customer_id,
        downstream_customer_id=row.downstream_customer_id,
        upstream_customer_name=row.upstream_customer_name,
        downstream_customer_name=row.downstream_customer_name,
        upstream_customer_deleted=row.upstream_customer_deleted,
        downstream_customer_deleted=row.downstream_customer_deleted,
        upstream_customer_name_source=row.upstream_customer_name_source,
        downstream_customer_name_source=row.downstream_customer_name_source,
        revenue=row.revenue,
        cost=row.cost,
        gross_profit=gross_profit,
        gross_margin=gross_margin,
        custom_values=dict(row.custom_values),
    )


async def search_records(
    session: AsyncSession,
    table_id: int,
    query: DataRecordSearch,
    *,
    context: AuthContext,
) -> DataSearchResult:
    """Search only rows selected by current pointers and visible to the caller."""
    _validate_query_shape(query)
    definition = await data_table_repository.load_resolved_definition(
        session,
        table_id,
    )
    if definition is None:
        raise _table_not_found()

    conditions = tuple(
        _normalize_condition(condition, definition=definition)
        for condition in query.conditions
    )
    sort_field = None
    if query.sort_field_id is not None:
        sort_field = definition.fields_by_id.get(query.sort_field_id)
        if sort_field is None:
            raise _invalid_query("Sort field does not belong to this data table")

    scope_customer_ids = (
        None if _is_unrestricted(context) else visible_customer_id_subquery(context)
    )
    page = await data_query_repository.search_current_records(
        session,
        definition=definition,
        conditions=conditions,
        sort_field=sort_field,
        sort_direction=query.sort_direction,
        page=query.page,
        page_size=query.page_size,
        date_from=query.date_from,
        date_to=query.date_to,
        upstream_customer_ids=_normalized_positive_ids(
            query.upstream_customer_ids,
            name="upstream_customer_ids",
        ),
        downstream_customer_ids=_normalized_positive_ids(
            query.downstream_customer_ids,
            name="downstream_customer_ids",
        ),
        scope_customer_ids=scope_customer_ids,
    )
    gross_profit = page.revenue - page.cost
    gross_margin = None if page.revenue == 0 else gross_profit / page.revenue
    summary = DataSearchSummary(
        row_count=page.total,
        revenue=page.revenue,
        cost=page.cost,
        gross_profit=gross_profit,
        gross_margin=gross_margin,
    )
    return DataSearchResult(
        items=[_line_from_repository(row) for row in page.items],
        total=page.total,
        page=query.page,
        page_size=query.page_size,
        summary=summary,
    )


async def list_table_visibility_summaries(
    session: AsyncSession,
    table_ids: Sequence[int],
    *,
    context: AuthContext,
) -> dict[int, TableVisibilitySummary]:
    """Return scoped current-row summaries for every requested table ID."""
    normalized_ids = _normalized_positive_ids(table_ids, name="table_ids")
    if not normalized_ids:
        return {}
    scope_customer_ids = (
        None if _is_unrestricted(context) else visible_customer_id_subquery(context)
    )
    aggregates = await data_query_repository.aggregate_table_visibility(
        session,
        table_ids=normalized_ids,
        scope_customer_ids=scope_customer_ids,
    )
    summaries: dict[int, TableVisibilitySummary] = {}
    for table_id in normalized_ids:
        aggregate = aggregates.get(table_id)
        summaries[table_id] = TableVisibilitySummary(
            row_count=0 if aggregate is None else aggregate.row_count,
            date_from=None if aggregate is None else aggregate.date_from,
            date_to=None if aggregate is None else aggregate.date_to,
        )
    return summaries


__all__: tuple[str, ...] = (
    "list_table_visibility_summaries",
    "search_records",
    "visible_customer_id_subquery",
)
