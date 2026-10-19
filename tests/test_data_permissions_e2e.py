"""End-to-end authorization coverage for current-data queries."""

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

from app.core.config import settings
from app.core.errors import ErrorCode
from app.models.audit_log import AuditLog
from app.models.role import DataScope
from app.services import audit as audit_service
from tests.customer_helpers import TEST_DATABASE_URL, session  # noqa: F401
from tests.data_management_e2e_helpers import (
    DataManagementE2EData,
    context_for,
    management_api_client,
    seed_management_e2e,
)


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
                delete(AuditLog).where(AuditLog.action.like("data_%"))
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


async def test_sales_scope_returns_full_row_when_either_customer_is_visible(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    context = context_for(e2e_data.sales, data_scope=DataScope.SELF)
    async with management_api_client(session, context) as client:
        response = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={
                "date_from": e2e_data.initial_date.isoformat(),
                "date_to": e2e_data.initial_date.isoformat(),
            },
        )

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 2
    upstream_visible = next(
        item
        for item in items
        if item["upstream_customer_id"] == e2e_data.sales_customer_id
    )
    downstream_visible = next(
        item
        for item in items
        if item["downstream_customer_id"] == e2e_data.sales_customer_id
    )
    assert upstream_visible["downstream_customer_id"] == e2e_data.hidden_customer_id
    assert upstream_visible["downstream_customer_name"] == e2e_data.hidden_customer_name
    assert upstream_visible["revenue"] == "100.00000000"
    assert downstream_visible["upstream_customer_id"] == e2e_data.hidden_customer_id
    assert downstream_visible["upstream_customer_name"] == e2e_data.hidden_customer_name
    assert downstream_visible["cost"] == "60.00000000"


async def test_unrelated_sales_user_gets_zero_rows_and_zero_summary(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    context = context_for(e2e_data.unrelated_sales, data_scope=DataScope.SELF)
    async with management_api_client(session, context) as client:
        response = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={},
        )

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["items"] == []
    assert result["total"] == 0
    assert result["summary"] == {
        "row_count": 0,
        "revenue": "0",
        "cost": "0",
        "gross_profit": "0",
        "gross_margin": None,
    }


async def test_query_and_table_list_require_data_read(
    session: AsyncSession,
    e2e_data: DataManagementE2EData,
) -> None:
    context = context_for(
        e2e_data.sales,
        data_scope=DataScope.SELF,
        permission_codes=set(),
    )
    async with management_api_client(session, context) as client:
        search = await client.post(
            f"/api/v1/data-tables/{e2e_data.table_id}/records/search",
            json={},
        )
        listing = await client.get("/api/v1/data-tables")

    assert search.status_code == 403
    assert search.json()["code"] == ErrorCode.FORBIDDEN
    assert listing.status_code == 403
    assert listing.json()["code"] == ErrorCode.FORBIDDEN
