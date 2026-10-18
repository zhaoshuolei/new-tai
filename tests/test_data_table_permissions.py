"""数据表定义 API 的权限边界测试。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.data_management import DataTableDefinition
from app.models.user import User
from app.schemas.data_table import DataTableCreate
from app.services import audit as audit_service
from app.services import data_table as data_table_service
from tests.conftest import FakeRedis
from tests.customer_helpers import TEST_DATABASE_URL, make_context, make_user

_AUDIT_ACTIONS = ("data_table:create", "data_table:update", "data_table:delete")
_ALL_DATA_PERMISSIONS = {
    "data:read",
    "data:create",
    "data:update",
    "data:delete",
    "data:import",
}


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
                delete(AuditLog).where(AuditLog.action.in_(_AUDIT_ACTIONS))
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest_asyncio.fixture
async def caller(session: AsyncSession) -> User:
    return await make_user(session)


def _payload(*, name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or f"权限表-{uuid4().hex[:8]}",
        "description": None,
        "fields": [
            {
                "label": "日期",
                "field_type": "DATE",
                "system_key": "BUSINESS_DATE",
            },
            {
                "label": "上游客户",
                "field_type": "CUSTOMER",
                "system_key": "UPSTREAM_CUSTOMER",
            },
            {
                "label": "下游客户",
                "field_type": "CUSTOMER",
                "system_key": "DOWNSTREAM_CUSTOMER",
            },
            {
                "label": "收入",
                "field_type": "DECIMAL",
                "system_key": "REVENUE",
            },
            {
                "label": "成本",
                "field_type": "DECIMAL",
                "system_key": "COST",
            },
        ],
    }


def _context(user: User, permission_code: str) -> AuthContext:
    return make_context(user, permission_codes={permission_code})


@asynccontextmanager
async def api_client(
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


async def _create_table(
    session: AsyncSession,
    caller: User,
    *,
    name: str | None = None,
) -> DataTableDefinition:
    context = make_context(caller, permission_codes=_ALL_DATA_PERMISSIONS)
    return await data_table_service.create_table(
        session,
        DataTableCreate.model_validate(_payload(name=name)),
        context=context,
    )


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/v1/data-tables", None),
        ("POST", "/api/v1/data-tables", _payload()),
        ("GET", "/api/v1/data-tables/1", None),
        ("PUT", "/api/v1/data-tables/1", _payload()),
        ("DELETE", "/api/v1/data-tables/1", None),
    ],
)
async def test_each_route_requires_a_data_permission(
    session: AsyncSession,
    caller: User,
    method: str,
    path: str,
    json: dict[str, Any] | None,
) -> None:
    context = make_context(caller)
    async with api_client(session, context) as client:
        response = await client.request(method, path, json=json)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == ErrorCode.FORBIDDEN


async def test_read_permission_allows_list_and_detail_but_not_create(
    session: AsyncSession,
    caller: User,
) -> None:
    table = await _create_table(session, caller)
    context = _context(caller, "data:read")

    async with api_client(session, context) as client:
        listing = await client.get("/api/v1/data-tables")
        detail = await client.get(f"/api/v1/data-tables/{table.id}")
        create = await client.post(
            "/api/v1/data-tables",
            json=_payload(),
        )

    assert listing.status_code == 200, listing.text
    assert table.id in {item["id"] for item in listing.json()["data"]}
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["id"] == table.id
    assert create.status_code == 403
    assert create.json()["code"] == ErrorCode.FORBIDDEN


async def test_create_permission_does_not_grant_read_or_delete(
    session: AsyncSession,
    caller: User,
) -> None:
    context = _context(caller, "data:create")

    async with api_client(session, context) as client:
        created = await client.post(
            "/api/v1/data-tables",
            json=_payload(),
        )
        listing = await client.get("/api/v1/data-tables")
        deleted = await client.delete(
            f"/api/v1/data-tables/{created.json()['data']['id']}"
        )

    assert created.status_code == 201, created.text
    assert listing.status_code == 403
    assert deleted.status_code == 403


async def test_update_permission_does_not_grant_create_or_delete(
    session: AsyncSession,
    caller: User,
) -> None:
    table = await _create_table(session, caller)
    context = _context(caller, "data:update")

    async with api_client(session, context) as client:
        updated = await client.put(
            f"/api/v1/data-tables/{table.id}",
            json=_payload(name="更新权限后的表"),
        )
        created = await client.post(
            "/api/v1/data-tables",
            json=_payload(),
        )
        deleted = await client.delete(f"/api/v1/data-tables/{table.id}")

    assert updated.status_code == 200, updated.text
    assert created.status_code == 403
    assert deleted.status_code == 403


async def test_delete_permission_does_not_grant_read_or_update(
    session: AsyncSession,
    caller: User,
) -> None:
    table = await _create_table(session, caller)
    context = _context(caller, "data:delete")

    async with api_client(session, context) as client:
        detail = await client.get(f"/api/v1/data-tables/{table.id}")
        updated = await client.put(
            f"/api/v1/data-tables/{table.id}",
            json=_payload(name="不应更新"),
        )
        deleted = await client.delete(f"/api/v1/data-tables/{table.id}")

    assert detail.status_code == 403
    assert updated.status_code == 403
    assert deleted.status_code == 200, deleted.text
