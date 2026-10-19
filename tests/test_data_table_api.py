"""数据表定义 HTTP API 测试。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.audit_meta import ActorSnapshot, get_audit_route_meta
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.user import User
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


def fixed_fields() -> list[dict[str, object]]:
    return [
        {
            "label": "日期",
            "field_type": "DATE",
            "system_key": "BUSINESS_DATE",
            "sort_order": 0,
        },
        {
            "label": "上游客户",
            "field_type": "CUSTOMER",
            "system_key": "UPSTREAM_CUSTOMER",
            "sort_order": 1,
        },
        {
            "label": "下游客户",
            "field_type": "CUSTOMER",
            "system_key": "DOWNSTREAM_CUSTOMER",
            "sort_order": 2,
        },
        {
            "label": "收入",
            "field_type": "DECIMAL",
            "system_key": "REVENUE",
            "sort_order": 3,
        },
        {
            "label": "成本",
            "field_type": "DECIMAL",
            "system_key": "COST",
            "sort_order": 4,
        },
    ]


def make_table_payload(
    *,
    name: str | None = None,
    custom_label: str = "渠道",
    custom_type: str = "ENUM",
) -> dict[str, object]:
    custom_field: dict[str, object] = {
        "label": custom_label,
        "field_type": custom_type,
        "system_key": "CUSTOM",
        "sort_order": 10,
    }
    if custom_type == "ENUM":
        custom_field["options"] = [
            {"label": "线上", "sort_order": 0},
            {"label": "线下", "sort_order": 1},
        ]
    return {
        "name": name or f"接口表-{uuid4().hex[:8]}",
        "description": "按日明细",
        "fields": [*fixed_fields(), custom_field],
    }


def _context(user: User) -> AuthContext:
    return make_context(user, permission_codes=_ALL_DATA_PERMISSIONS)


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext | None,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = FakeRedis
    if context is not None:

        async def override_context(request: Request) -> AuthContext:
            request.state.user_id = context.user_id
            request.state.actor = ActorSnapshot(
                user_id=context.user_id,
                username=context.username,
                nickname=context.nickname,
            )
            return context

        application.dependency_overrides[get_current_context] = override_context

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


async def create_definition(
    client: AsyncClient,
    *,
    name: str | None = None,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/data-tables",
        json=make_table_payload(name=name),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


async def test_create_definition_returns_complete_envelope(
    session: AsyncSession,
    caller: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_get_table(*args: object, **kwargs: object) -> None:
        raise AssertionError("create must serialize the service write result")

    monkeypatch.setattr(data_table_service, "get_table", unexpected_get_table)

    async with api_client(session, _context(caller)) as client:
        response = await client.post(
            "/api/v1/data-tables",
            json=make_table_payload(name="客户投放日明细"),
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "created"
    data = body["data"]
    assert data["id"]
    assert data["name"] == "客户投放日明细"
    assert data["description"] == "按日明细"
    assert data["status"] == "DRAFT"
    assert data["created_by"] == caller.id
    assert data["updated_by"] is None
    assert data["locked_at"] is None
    assert data["created_at"].endswith("+00:00")
    assert data["updated_at"].endswith("+00:00")
    assert [field["label"] for field in data["fields"]] == [
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
        "渠道",
    ]
    assert data["fields"][-1]["options"] == [
        {
            "id": data["fields"][-1]["options"][0]["id"],
            "label": "线上",
            "sort_order": 0,
        },
        {
            "id": data["fields"][-1]["options"][1]["id"],
            "label": "线下",
            "sort_order": 1,
        },
    ]


async def test_list_and_detail_return_resolved_definitions(
    session: AsyncSession,
    caller: User,
) -> None:
    async with api_client(session, _context(caller)) as client:
        created = await create_definition(client)
        detail = await client.get(f"/api/v1/data-tables/{created['id']}")
        listing = await client.get("/api/v1/data-tables")

    assert detail.status_code == 200, detail.text
    assert detail.json()["code"] == 0
    assert detail.json()["data"] == created
    assert listing.status_code == 200, listing.text
    listing_body = listing.json()
    assert listing_body["code"] == 0
    assert listing_body["message"] == "ok"
    assert listing_body["data"] == [
        {
            **created,
            "visibility": {
                "row_count": 0,
                "date_from": None,
                "date_to": None,
            },
        }
    ]


async def test_replace_definition_returns_replaced_fields(
    session: AsyncSession,
    caller: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with api_client(session, _context(caller)) as client:
        created = await create_definition(client)

        async def unexpected_get_table(*args: object, **kwargs: object) -> None:
            raise AssertionError("replace must serialize the service write result")

        monkeypatch.setattr(data_table_service, "get_table", unexpected_get_table)
        response = await client.put(
            f"/api/v1/data-tables/{created['id']}",
            json=make_table_payload(
                name="替换后的明细表",
                custom_label="备注",
                custom_type="TEXT",
            ),
        )

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert data["id"] == created["id"]
    assert data["name"] == "替换后的明细表"
    assert data["updated_by"] == caller.id
    assert data["fields"][-1]["label"] == "备注"
    assert data["fields"][-1]["field_type"] == "TEXT"
    assert data["fields"][-1]["options"] == []


async def test_pre_service_permission_denial_writes_one_denied_audit_row(
    session: AsyncSession,
    caller: User,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_client(session, _context(caller)) as client:
        created = await create_definition(client)

    async with audit_session_factory() as cleanup_session:
        await cleanup_session.execute(
            delete(AuditLog).where(AuditLog.action.in_(_AUDIT_ACTIONS))
        )
        await cleanup_session.commit()

    denied_context = make_context(caller)
    async with api_client(session, denied_context) as client:
        response = await client.put(
            f"/api/v1/data-tables/{created['id']}",
            json=make_table_payload(name="无权替换"),
        )

    assert response.status_code == 403, response.text
    async with audit_session_factory() as audit_session:
        rows = list(
            (
                await audit_session.scalars(
                    select(AuditLog)
                    .where(AuditLog.action == "data_table:update")
                    .order_by(AuditLog.id)
                )
            ).all()
        )

    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].action == "data_table:update"
    assert rows[0].resource_type == "custom_data_table"
    assert rows[0].resource_id == created["id"]
    assert rows[0].reason == "FORBIDDEN"
    assert rows[0].user_id == caller.id


async def test_delete_definition_returns_null_and_detail_becomes_not_found(
    session: AsyncSession,
    caller: User,
) -> None:
    async with api_client(session, _context(caller)) as client:
        created = await create_definition(client)
        response = await client.delete(f"/api/v1/data-tables/{created['id']}")
        detail = await client.get(f"/api/v1/data-tables/{created['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == {"code": 0, "message": "deleted", "data": None}
    assert detail.status_code == 404
    assert detail.json()["code"] == ErrorCode.DATA_TABLE_NOT_FOUND


async def test_invalid_definition_is_rejected_by_service(
    session: AsyncSession,
    caller: User,
) -> None:
    payload = make_table_payload()
    payload["name"] = "   "

    async with api_client(session, _context(caller)) as client:
        response = await client.post("/api/v1/data-tables", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_TABLE_INVALID


async def test_definition_routes_require_authentication(
    session: AsyncSession,
) -> None:
    async with api_client(session, None) as client:
        response = await client.get("/api/v1/data-tables")

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.UNAUTHENTICATED


def test_definition_routes_are_registered_for_audit() -> None:
    expected = {
        ("POST", "/data-tables"): ("data_table:create", None),
        ("PUT", "/data-tables/{table_id}"): ("data_table:update", "table_id"),
        ("DELETE", "/data-tables/{table_id}"): ("data_table:delete", "table_id"),
    }

    for (method, path), (action, resource_id_param) in expected.items():
        meta = get_audit_route_meta(method, path)
        assert meta is not None
        assert meta.action == action
        assert meta.resource_type == "custom_data_table"
        assert meta.resource_id_param == resource_id_param


def test_definition_routes_use_data_management_openapi_tag() -> None:
    document = create_app().openapi()
    paths = document["paths"]
    data_tag = next(tag for tag in document["tags"] if tag["name"] == "数据管理")
    assert data_tag["description"] == "自定义数据表定义、导入、模板与当前数据管理"
    expected = [
        ("get", "/api/v1/data-tables"),
        ("post", "/api/v1/data-tables"),
        ("get", "/api/v1/data-tables/{table_id}"),
        ("put", "/api/v1/data-tables/{table_id}"),
        ("delete", "/api/v1/data-tables/{table_id}"),
        ("post", "/api/v1/data-tables/{table_id}/records/search"),
    ]

    for method, path in expected:
        assert paths[path][method]["tags"] == ["数据管理"]
