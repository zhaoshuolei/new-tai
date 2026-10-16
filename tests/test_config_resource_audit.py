import os
from collections.abc import AsyncIterator

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
from app.models.menu import MenuType
from app.models.role import DataScope, Role, RoleStatus
from app.schemas.department import DepartmentCreate
from app.schemas.menu import MenuCreate, MenuUpdate
from app.schemas.role import RoleCreate
from app.services import audit as audit_service
from app.services.department import create_department, move_department
from app.services.menu import create_menu, update_menu
from app.services.role import create_role, delete_role

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


def context_with_permissions(
    *permission_codes: str,
    is_super_admin: bool = False,
) -> AuthContext:
    return AuthContext(
        user_id=1,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=is_super_admin,
        username="admin",
        nickname="管理员",
    )


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
) -> list[AuditLog]:
    async with factory() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
        return list(rows.all())


async def add_system_role(session: AsyncSession) -> Role:
    role = Role(
        code="super_admin",
        name="超级管理员",
        description=None,
        data_scope=DataScope.ALL,
        data_scope_include_children=True,
        is_system=True,
        status=RoleStatus.ACTIVE,
    )
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return role


# --- role -----------------------------------------------------------------


async def test_post_role_success_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("role:create")

    role = await create_role(
        session,
        RoleCreate(code="operator", name="运营"),
        context=context,
    )

    rows = await audit_rows(audit_session_factory, action="role:create")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "role"
    assert rows[0].resource_id == role.id
    assert rows[0].reason is None
    assert rows[0].user_id == context.user_id
    assert rows[0].actor_username == "admin"


async def test_delete_system_role_denied_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    role = await add_system_role(session)
    context = context_with_permissions("role:delete", is_super_admin=True)

    with pytest.raises(BusinessError) as error:
        await delete_role(session, role.id, context=context)

    assert error.value.code == ErrorCode.SYSTEM_ROLE_IMMUTABLE
    rows = await audit_rows(audit_session_factory, action="role:delete")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "role"
    assert rows[0].resource_id == role.id
    assert rows[0].reason == "SYSTEM_ROLE_IMMUTABLE"


# --- menu -----------------------------------------------------------------


async def test_post_menu_success_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("menu:create")

    menu = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
        context=context,
    )

    rows = await audit_rows(audit_session_factory, action="menu:create")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "menu"
    assert rows[0].resource_id == menu.id
    assert rows[0].reason is None
    assert rows[0].user_id == context.user_id
    assert rows[0].actor_username == "admin"


async def test_update_menu_invalid_hierarchy_denied_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("menu:update")
    directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    menu = await create_menu(
        session,
        MenuCreate(
            parent_id=directory.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )
    button = await create_menu(
        session,
        MenuCreate(
            parent_id=menu.id,
            type=MenuType.BUTTON,
            name="新增用户",
        ),
    )

    with pytest.raises(BusinessError) as error:
        await update_menu(
            session,
            button.id,
            MenuUpdate(parent_id=directory.id),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY
    rows = await audit_rows(audit_session_factory, action="menu:update")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "menu"
    assert rows[0].resource_id == button.id
    assert rows[0].reason == "INVALID_MENU_HIERARCHY"


# --- department -----------------------------------------------------------


async def test_post_department_success_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("dept:create")

    department = await create_department(
        session,
        DepartmentCreate(name="总部"),
        context=context,
    )

    rows = await audit_rows(audit_session_factory, action="dept:create")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "department"
    assert rows[0].resource_id == department.id
    assert rows[0].reason is None
    assert rows[0].user_id == context.user_id
    assert rows[0].actor_username == "admin"


async def test_move_department_invalid_target_denied_records_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = context_with_permissions("dept:update")
    department = await create_department(session, DepartmentCreate(name="总部"))

    with pytest.raises(BusinessError) as error:
        await move_department(
            session,
            department.id,
            department.id,
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_DEPT_MOVE
    rows = await audit_rows(audit_session_factory, action="dept:move")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "department"
    assert rows[0].resource_id == department.id
    assert rows[0].reason == "INVALID_DEPT_MOVE"


# --- route registration / dependency-level denials ------------------------


async def make_denial_request(
    session: AsyncSession,
    method: str,
    path: str,
) -> int:
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
            response = await client.request(method, path)
    finally:
        application.dependency_overrides.clear()
    return response.status_code


async def test_role_route_registration_records_dependency_denial(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    status_code = await make_denial_request(
        session,
        "DELETE",
        "/api/v1/roles/77",
    )

    assert status_code == 403
    rows = await audit_rows(audit_session_factory, action="role:delete")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "role"
    assert rows[0].resource_id == 77
    assert rows[0].reason == "FORBIDDEN"


async def test_menu_route_registration_records_dependency_denial(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    status_code = await make_denial_request(
        session,
        "DELETE",
        "/api/v1/menus/88",
    )

    assert status_code == 403
    rows = await audit_rows(audit_session_factory, action="menu:delete")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "menu"
    assert rows[0].resource_id == 88
    assert rows[0].reason == "FORBIDDEN"


async def test_department_route_registration_records_dependency_denial(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    status_code = await make_denial_request(
        session,
        "PATCH",
        "/api/v1/departments/99/move",
    )

    assert status_code == 403
    rows = await audit_rows(audit_session_factory, action="dept:move")
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].resource_type == "department"
    assert rows[0].resource_id == 99
    assert rows[0].reason == "FORBIDDEN"
