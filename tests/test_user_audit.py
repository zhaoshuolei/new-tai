import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.schemas.user import UserCreate, UserRoleUpdate
from app.services import audit as audit_service
from app.services.user import assign_user_role, create_local_user

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


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as session:
        rows = await session.scalars(select(AuditLog).order_by(AuditLog.id))
        return list(rows.all())


async def test_create_success_records_audit_row_with_new_id(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("user:create")

    user = await create_local_user(
        session,
        UserCreate(
            username=f"audit-create-{uuid4().hex[:12]}",
            password="audit-password1",
        ),
        context=context,
    )

    rows = [
        row
        for row in await audit_rows(audit_session_factory)
        if row.action == "user:create"
    ]
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "user"
    assert rows[0].resource_id == user.id
    assert rows[0].reason is None
    assert rows[0].user_id == context.user_id


async def test_create_without_context_records_null_actor(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_local_user(
        session,
        UserCreate(
            username=f"audit-nullactor-{uuid4().hex[:12]}",
            password="audit-password1",
        ),
    )

    rows = [
        row
        for row in await audit_rows(audit_session_factory)
        if row.action == "user:create"
    ]
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_id == user.id
    assert rows[0].user_id is None
    assert rows[0].actor_username is None


async def test_assign_self_denied_records_exactly_one_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_local_user(
        session,
        UserCreate(
            username=f"audit-self-{uuid4().hex[:12]}",
            password="audit-password1",
        ),
    )
    context = AuthContext(
        user_id=user.id,
        role_id=None,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset({"user:assign"}),
        is_super_admin=False,
        username=user.username,
        nickname=user.nickname,
    )

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            user.id,
            UserRoleUpdate(role_id=None),
            context=context,
        )

    assert error.value.code == ErrorCode.CANNOT_MODIFY_SELF
    rows = [
        row
        for row in await audit_rows(audit_session_factory)
        if row.action == "user:assign"
    ]
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "user"
    assert rows[0].resource_id == user.id
    assert rows[0].reason == "CANNOT_MODIFY_SELF"


async def test_create_without_permission_records_dependency_denial(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app()
    context = context_with_permissions()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return context

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/users",
                json={
                    "username": f"audit-denied-{uuid4().hex[:12]}",
                    "password": "audit-password1",
                },
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 403
    rows = [
        row
        for row in await audit_rows(audit_session_factory)
        if row.action == "user:create"
    ]
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].reason == "FORBIDDEN"
    assert rows[0].resource_type == "user"
    assert rows[0].resource_id is None
