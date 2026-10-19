"""Shared setup for custom-data management API end-to-end tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.data_management import DataImportSource, DataImportStatus
from app.models.role import DataScope
from app.models.user import User
from app.repositories import data_table as data_table_repository
from app.repositories.data_table import ResolvedDataDefinition
from app.services import data_import as data_import_service
from app.services.data_import_parser import (
    ValidatedImportRow,
    load_customer_name_index,
    validate_raw_row,
)
from app.services.data_import_storage import save_import_bytes
from tests.conftest import FakeRedis
from tests.customer_helpers import make_context, make_customer_row, make_user

_ALL_DATA_PERMISSIONS = {
    "data:read",
    "data:create",
    "data:update",
    "data:delete",
    "data:import",
}
_FIXED_FIELDS: tuple[dict[str, object], ...] = (
    {
        "label": "Business Date",
        "field_type": "DATE",
        "system_key": "BUSINESS_DATE",
        "sort_order": 0,
    },
    {
        "label": "Upstream Customer",
        "field_type": "CUSTOMER",
        "system_key": "UPSTREAM_CUSTOMER",
        "sort_order": 1,
    },
    {
        "label": "Downstream Customer",
        "field_type": "CUSTOMER",
        "system_key": "DOWNSTREAM_CUSTOMER",
        "sort_order": 2,
    },
    {
        "label": "Revenue",
        "field_type": "DECIMAL",
        "system_key": "REVENUE",
        "sort_order": 3,
    },
    {
        "label": "Cost",
        "field_type": "DECIMAL",
        "system_key": "COST",
        "sort_order": 4,
    },
)
_CUSTOM_FIELDS: tuple[dict[str, object], ...] = (
    {
        "label": "Text Value",
        "field_type": "TEXT",
        "system_key": "CUSTOM",
        "sort_order": 10,
    },
    {
        "label": "Integer Value",
        "field_type": "INTEGER",
        "system_key": "CUSTOM",
        "sort_order": 11,
    },
    {
        "label": "Decimal Value",
        "field_type": "DECIMAL",
        "system_key": "CUSTOM",
        "sort_order": 12,
    },
    {
        "label": "Date Value",
        "field_type": "DATE",
        "system_key": "CUSTOM",
        "sort_order": 13,
    },
    {
        "label": "Boolean Value",
        "field_type": "BOOLEAN",
        "system_key": "CUSTOM",
        "sort_order": 14,
    },
    {
        "label": "Enum Value",
        "field_type": "ENUM",
        "system_key": "CUSTOM",
        "sort_order": 15,
        "options": [
            {"label": "Online", "sort_order": 0},
            {"label": "Offline", "sort_order": 1},
        ],
    },
)


@dataclass(frozen=True)
class DataManagementE2EData:
    table_id: int
    empty_table_id: int
    ops: User
    sales: User
    unrelated_sales: User
    sales_customer_id: int
    hidden_customer_id: int
    hidden_customer_name: str
    fields: Mapping[str, Mapping[str, Any]]
    initial_date: date
    replacement_date: date


@asynccontextmanager
async def management_api_client(
    session: AsyncSession,
    context: AuthContext,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context(request: Request) -> AuthContext:
        request.state.user_id = context.user_id
        request.state.actor = ActorSnapshot(
            user_id=context.user_id,
            username=context.username,
            nickname=context.nickname,
        )
        return context

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_redis] = FakeRedis
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


def context_for(
    user: User,
    *,
    data_scope: DataScope,
    permission_codes: set[str] | None = None,
) -> AuthContext:
    return make_context(
        user,
        permission_codes=(
            _ALL_DATA_PERMISSIONS if permission_codes is None else permission_codes
        ),
        data_scope=data_scope,
        role_id=10,
    )


def _table_payload(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": "Task 12 end-to-end table",
        "fields": [*_FIXED_FIELDS, *_CUSTOM_FIELDS],
    }


async def create_table_via_api(
    client: AsyncClient,
    *,
    name: str,
) -> dict[str, Any]:
    response = await client.post("/api/v1/data-tables", json=_table_payload(name))
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


def fields_by_label(table: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(field["label"]): field for field in table["fields"]}


def _custom_values(
    fields: Mapping[str, Mapping[str, Any]],
    *,
    text: str,
    integer: int,
    decimal: str,
    custom_date: date,
    boolean: bool,
    enum_label: str,
) -> dict[str, object]:
    return {
        str(fields["Text Value"]["id"]): text,
        str(fields["Integer Value"]["id"]): integer,
        str(fields["Decimal Value"]["id"]): decimal,
        str(fields["Date Value"]["id"]): custom_date.isoformat(),
        str(fields["Boolean Value"]["id"]): boolean,
        str(fields["Enum Value"]["id"]): enum_label,
    }


def build_initial_rows(
    data: DataManagementE2EData,
) -> list[dict[str, object]]:
    custom = _custom_values(
        data.fields,
        text="enterprise",
        integer=7,
        decimal="12.34000000",
        custom_date=date(2026, 10, 1),
        boolean=True,
        enum_label="Online",
    )
    return [
        {
            "business_date": data.initial_date.isoformat(),
            "upstream_customer_id": data.sales_customer_id,
            "downstream_customer_id": data.hidden_customer_id,
            "revenue": "100.00000000",
            "cost": "40.00000000",
            "custom_values": custom,
        },
        {
            "business_date": data.initial_date.isoformat(),
            "upstream_customer_id": data.hidden_customer_id,
            "downstream_customer_id": data.sales_customer_id,
            "revenue": "50.00000000",
            "cost": "60.00000000",
        },
        {
            "business_date": data.initial_date.isoformat(),
            "upstream_customer_id": data.hidden_customer_id,
            "downstream_customer_id": data.hidden_customer_id,
            "revenue": "30.00000000",
            "cost": "10.00000000",
        },
        {
            "business_date": data.replacement_date.isoformat(),
            "upstream_customer_id": data.sales_customer_id,
            "downstream_customer_id": data.hidden_customer_id,
            "revenue": "200.00000000",
            "cost": "80.00000000",
        },
    ]


def build_replacement_rows(
    data: DataManagementE2EData,
) -> list[dict[str, object]]:
    return [
        {
            "business_date": data.replacement_date.isoformat(),
            "upstream_customer_id": data.sales_customer_id,
            "downstream_customer_id": data.hidden_customer_id,
            "revenue": "250.00000000",
            "cost": "100.00000000",
        }
    ]


async def import_rows(
    session: AsyncSession,
    *,
    definition: ResolvedDataDefinition,
    rows: Sequence[Mapping[str, object]],
    context: AuthContext,
) -> None:
    customer_index = await load_customer_name_index(session)
    validated_rows = []
    for raw in rows:
        parsed = validate_raw_row(raw, definition, customer_index, None)
        assert not isinstance(parsed, list), parsed
        validated_rows.append(parsed)

    source = json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")
    stored = save_import_bytes(source, suffix=".json", original_name="source.json")
    job = await data_import_service.enqueue_import(
        session,
        definition.table.id,
        source=DataImportSource.API,
        stored=stored,
        context=context,
    )
    worker_id = f"task-12-{uuid4().hex}"
    job.status = DataImportStatus.VALIDATING
    job.attempt_count = 1
    job.lease_owner = worker_id
    job.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    await session.commit()

    rows_by_date: dict[date, list[ValidatedImportRow]] = {}
    for row in validated_rows:
        rows_by_date.setdefault(row.business_date, []).append(row)
    snapshots = await data_import_service.start_staged_import(
        session,
        job.id,
        rows_by_date,
        worker_id=worker_id,
    )
    for business_date, dated_rows in rows_by_date.items():
        await data_import_service.append_staged_rows(
            session,
            snapshots[business_date].id,
            dated_rows,
            worker_id=worker_id,
        )
    await data_import_service.activate_import(
        session,
        import_id=job.id,
        worker_id=worker_id,
        expected_dates=rows_by_date,
    )


async def load_definition(
    session: AsyncSession,
    table_id: int,
) -> ResolvedDataDefinition:
    definition = await data_table_repository.load_resolved_definition(session, table_id)
    assert definition is not None
    return definition


async def seed_management_e2e(
    session: AsyncSession,
) -> DataManagementE2EData:
    ops = await make_user(session)
    sales = await make_user(session)
    unrelated = await make_user(session)
    hidden_owner = await make_user(session)
    sales_customer = await make_customer_row(
        session,
        owner=sales,
        name=f"sales-{uuid4().hex}",
    )
    hidden_customer = await make_customer_row(
        session,
        owner=hidden_owner,
        name=f"hidden-{uuid4().hex}",
    )
    ops_context = context_for(ops, data_scope=DataScope.ALL)
    async with management_api_client(session, ops_context) as client:
        table = await create_table_via_api(
            client,
            name=f"task-12-{uuid4().hex}",
        )
        empty_table = await create_table_via_api(
            client,
            name=f"task-12-empty-{uuid4().hex}",
        )

    data = DataManagementE2EData(
        table_id=int(table["id"]),
        empty_table_id=int(empty_table["id"]),
        ops=ops,
        sales=sales,
        unrelated_sales=unrelated,
        sales_customer_id=sales_customer.id,
        hidden_customer_id=hidden_customer.id,
        hidden_customer_name=hidden_customer.name,
        fields=fields_by_label(table),
        initial_date=date(2026, 10, 17),
        replacement_date=date(2026, 10, 18),
    )
    definition = await load_definition(session, data.table_id)
    await import_rows(
        session,
        definition=definition,
        rows=build_initial_rows(data),
        context=ops_context,
    )
    return data


__all__: tuple[str, ...] = (
    "DataManagementE2EData",
    "build_initial_rows",
    "build_replacement_rows",
    "context_for",
    "create_table_via_api",
    "fields_by_label",
    "import_rows",
    "load_definition",
    "management_api_client",
    "seed_management_e2e",
)
