"""End-to-end current-data query and table visibility API tests."""

# ruff: noqa: F811

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.audit_meta import get_audit_route_meta
from app.core.config import settings
from app.core.errors import ErrorCode
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.role import DataScope
from app.services import audit as audit_service
from tests.customer_helpers import TEST_DATABASE_URL, session  # noqa: F401
from tests.data_management_e2e_helpers import (
    DataManagementE2EData,
    build_replacement_rows,
    context_for,
    import_rows,
    load_definition,
    management_api_client,
    seed_management_e2e,
)

_CLEANED_AUDIT_PREFIX = "data_"


@pytest.fixture(autouse=True)
def configure_import_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path / "data-imports"))


@pytest_asyncio.fixture(autouse=True)
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(
                delete(AuditLog).where(
                    AuditLog.action.like(f"{_CLEANED_AUDIT_PREFIX}%")
                )
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest_asyncio.fixture
async def e2e_data(session: AsyncSession) -> DataManagementE2EData:  # noqa: F811
    return await seed_management_e2e(session)


async def _replace_replacement_date(
    session: AsyncSession,
    data: DataManagementE2EData,
) -> None:
    definition = await load_definition(session, data.table_id)
    await import_rows(
        session,
        definition=definition,
        rows=build_replacement_rows(data),
        context=context_for(data.ops, data_scope=DataScope.ALL),
    )


async def test_search_returns_current_typed_rows_after_single_date_replacement(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    ops_context = context_for(e2e_data.ops, data_scope=DataScope.ALL)
    async with management_api_client(session, ops_context) as client:
        initial = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={},
        )
        assert initial.status_code == 200, initial.text
        initial_items = initial.json()["data"]["items"]
        initial_ids_by_date: dict[str, set[int]] = {}
        for item in initial_items:
            initial_ids_by_date.setdefault(item["business_date"], set()).add(item["id"])
        superseded_id = next(
            item["id"]
            for item in initial_items
            if item["business_date"] == e2e_data.replacement_date.isoformat()
        )

    await _replace_replacement_date(session, e2e_data)

    async with management_api_client(session, ops_context) as client:
        response = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={"page": 1, "page_size": 100},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    result = body["data"]
    assert result["total"] == 4
    assert result["page"] == 1
    assert result["page_size"] == 100
    assert {item["business_date"] for item in result["items"]} == {
        e2e_data.initial_date.isoformat(),
        e2e_data.replacement_date.isoformat(),
    }
    assert superseded_id not in {item["id"] for item in result["items"]}
    assert {
        item["id"]
        for item in result["items"]
        if item["business_date"] == e2e_data.initial_date.isoformat()
    } == initial_ids_by_date[e2e_data.initial_date.isoformat()]

    replacement = next(
        item
        for item in result["items"]
        if item["business_date"] == e2e_data.replacement_date.isoformat()
    )
    assert replacement["revenue"] == "250.00000000"
    assert replacement["cost"] == "100.00000000"
    assert replacement["gross_profit"] == "150.00000000"
    assert result["summary"] == {
        "row_count": 4,
        "revenue": "430.00000000",
        "cost": "210.00000000",
        "gross_profit": "220.00000000",
        "gross_margin": "0.5116279069767441860465116279",
    }

    typed_row = next(
        item
        for item in result["items"]
        if item["custom_values"].get(str(e2e_data.fields["Text Value"]["id"]))
        == "enterprise"
    )
    enum_field = e2e_data.fields["Enum Value"]
    online_option = next(
        option for option in enum_field["options"] if option["label"] == "Online"
    )
    assert typed_row["business_date"] == e2e_data.initial_date.isoformat()
    assert typed_row["revenue"] == "100.00000000"
    assert typed_row["cost"] == "40.00000000"
    assert typed_row["custom_values"] == {
        str(e2e_data.fields["Text Value"]["id"]): "enterprise",
        str(e2e_data.fields["Integer Value"]["id"]): 7,
        str(e2e_data.fields["Decimal Value"]["id"]): "12.34000000",
        str(e2e_data.fields["Date Value"]["id"]): "2026-10-01",
        str(e2e_data.fields["Boolean Value"]["id"]): True,
        str(enum_field["id"]): {"id": online_option["id"], "label": "Online"},
    }


async def test_search_pagination_and_summary_cover_full_filtered_set(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    await _replace_replacement_date(session, e2e_data)
    sales_context = context_for(e2e_data.sales, data_scope=DataScope.SELF)
    async with management_api_client(session, sales_context) as client:
        first = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={"page": 1, "page_size": 2},
        )
        second = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={"page": 2, "page_size": 2},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert len(first_data["items"]) == 2
    assert first_data["total"] == 3
    assert len(second_data["items"]) == 1
    assert second_data["total"] == 3
    assert first_data["page"] == 1
    assert second_data["page"] == 2
    assert (
        first_data["summary"]
        == second_data["summary"]
        == {
            "row_count": 3,
            "revenue": "400.00000000",
            "cost": "200.00000000",
            "gross_profit": "200.00000000",
            "gross_margin": "0.5",
        }
    )


async def test_table_list_includes_all_definitions_with_scoped_visibility(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    await _replace_replacement_date(session, e2e_data)
    expected = {
        "ops": {
            e2e_data.table_id: {
                "row_count": 4,
                "date_from": e2e_data.initial_date.isoformat(),
                "date_to": e2e_data.replacement_date.isoformat(),
            },
            e2e_data.empty_table_id: {
                "row_count": 0,
                "date_from": None,
                "date_to": None,
            },
        },
        "sales": {
            e2e_data.table_id: {
                "row_count": 3,
                "date_from": e2e_data.initial_date.isoformat(),
                "date_to": e2e_data.replacement_date.isoformat(),
            },
            e2e_data.empty_table_id: {
                "row_count": 0,
                "date_from": None,
                "date_to": None,
            },
        },
        "unrelated": {
            e2e_data.table_id: {
                "row_count": 0,
                "date_from": None,
                "date_to": None,
            },
            e2e_data.empty_table_id: {
                "row_count": 0,
                "date_from": None,
                "date_to": None,
            },
        },
    }
    contexts = {
        "ops": context_for(e2e_data.ops, data_scope=DataScope.ALL),
        "sales": context_for(e2e_data.sales, data_scope=DataScope.SELF),
        "unrelated": context_for(
            e2e_data.unrelated_sales,
            data_scope=DataScope.SELF,
        ),
    }

    for caller, context in contexts.items():
        async with management_api_client(session, context) as client:
            response = await client.get("/api/v1/data-tables")
        assert response.status_code == 200, response.text
        items = response.json()["data"]
        assert {item["id"] for item in items} == {
            e2e_data.table_id,
            e2e_data.empty_table_id,
        }
        assert {item["id"]: item["visibility"] for item in items} == expected[caller]
        assert all("name" in item and "fields" in item for item in items)


async def test_search_request_validation_and_query_error_envelope(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    context = context_for(e2e_data.sales, data_scope=DataScope.SELF)
    async with management_api_client(session, context) as client:
        invalid_type = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={"page_size": 0},
        )
        invalid_filter = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={
                "conditions": [
                    {
                        "field_id": e2e_data.fields["Text Value"]["id"],
                        "operator": "gte",
                        "value": "enterprise",
                    }
                ]
            },
        )

    assert invalid_type.status_code == 422
    assert invalid_type.json()["code"] == ErrorCode.INVALID_PARAMETER
    assert invalid_filter.status_code == 422
    assert invalid_filter.json()["code"] == ErrorCode.DATA_QUERY_INVALID


def test_search_is_read_only_and_no_history_endpoint_is_exposed() -> None:
    document = create_app().openapi()
    search_path = "/api/v1/data-tables/{table_id}/records/search"
    assert search_path in document["paths"]
    assert "post" in document["paths"][search_path]
    data_table_paths = [
        path for path in document["paths"] if path.startswith("/api/v1/data-tables/")
    ]
    assert not any(
        marker in path.lower()
        for path in data_table_paths
        for marker in ("snapshot", "history")
    )
    assert (
        get_audit_route_meta("POST", "/data-tables/{table_id}/records/search") is None
    )
