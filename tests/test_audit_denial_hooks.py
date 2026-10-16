import os
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.exception_handlers import business_error_handler
from app.core.permissions import require_permission
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.services import audit as audit_service

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@pytest_asyncio.fixture
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


def context_with_permissions(*permission_codes: str) -> AuthContext:
    return AuthContext(
        user_id=1,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
        username="admin",
        nickname="管理员",
    )


def make_client(app: FastAPI, *, context: AuthContext | None = None) -> AsyncClient:
    if context is not None:

        async def override_context() -> AuthContext:
            return context

        app.dependency_overrides[get_current_context] = override_context

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as session:
        rows = await session.scalars(select(AuditLog).order_by(AuditLog.id))
        return list(rows.all())


async def test_service_denial_recorded_once(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app()
    context = context_with_permissions("user:update")

    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def update_user(*, context: AuthContext, user_id: int) -> None:
        raise BusinessError(
            ErrorCode.CANNOT_MODIFY_SELF,
            "Cannot modify self",
            http_status=403,
        )

    @app.patch("/audit-service/{user_id}")
    async def service_route(
        user_id: int,
        _context: Annotated[AuthContext, Depends(get_current_context)],
    ) -> dict[str, int]:
        await update_user(context=_context, user_id=user_id)
        return {"code": 0}

    register_audit_route(
        "PATCH",
        "/audit-service/{user_id}",
        action="user:update",
        resource_type="user",
    )

    async with make_client(app, context=context) as client:
        response = await client.patch("/audit-service/7")

    assert response.status_code == 403
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].action == "user:update"
    assert rows[0].resource_type == "user"
    assert rows[0].resource_id == 7
    assert rows[0].reason == "CANNOT_MODIFY_SELF"


async def test_dependency_denial_recorded(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app()
    context = context_with_permissions()
    service_ran = False

    @app.patch(
        "/audit-dependency/{user_id}",
        dependencies=[Depends(require_permission("user:update"))],
    )
    async def dependency_route(user_id: int) -> dict[str, int]:
        nonlocal service_ran
        service_ran = True
        return {"code": 0, "user_id": user_id}

    register_audit_route(
        "PATCH",
        "/audit-dependency/{user_id}",
        action="user:update",
        resource_type="user",
    )

    async with make_client(app, context=context) as client:
        response = await client.patch("/audit-dependency/9")

    assert response.status_code == 403
    assert service_ran is False
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].action == "user:update"
    assert rows[0].resource_type == "user"
    assert rows[0].resource_id == 9
    assert rows[0].reason == "FORBIDDEN"


async def test_non_denial_not_recorded(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app()

    @app.get("/audit-not-found/{user_id}")
    async def not_found_route(user_id: int) -> None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    register_audit_route(
        "GET",
        "/audit-not-found/{user_id}",
        action="user:read",
        resource_type="user",
    )

    async with make_client(app) as client:
        response = await client.get("/audit-not-found/7")

    assert response.status_code == 404
    assert await audit_rows(audit_session_factory) == []


async def test_missing_matched_route_does_not_crash(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/v1/users/7",
            "raw_path": b"/api/v1/users/7",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )
    error = BusinessError(ErrorCode.FORBIDDEN, "Denied", http_status=403)

    response = await business_error_handler(request, error)

    assert response.status_code == 403
    assert await audit_rows(audit_session_factory) == []
