import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.menu import MenuStatus, MenuType
from app.models.role import DataScope, Role, RoleStatus
from app.schemas.menu import MenuCreate, MenuUpdate
from app.schemas.role import RoleCreate, RoleUpdate
from app.services.menu import create_menu, update_menu
from app.services.role import assign_role_menus, create_role, update_role

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


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
    role_id: int | None = 1,
    is_super_admin: bool = False,
) -> AuthContext:
    return AuthContext(
        user_id=1,
        role_id=role_id,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=is_super_admin,
    )


def make_client(
    session: AsyncSession,
    *,
    context: AuthContext | None = None,
) -> AsyncClient:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    if context is not None:

        async def override_context() -> AuthContext:
            return context

        application.dependency_overrides[get_current_context] = override_context

    return AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    )


async def add_super_admin_role(session: AsyncSession) -> Role:
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


async def test_endpoint_requires_declared_permission(
    session: AsyncSession,
) -> None:
    client = make_client(
        session,
        context=context_with_permissions(),
    )
    async with client:
        response = await client.get("/api/v1/users")

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.FORBIDDEN


async def test_endpoint_passes_with_permission(
    session: AsyncSession,
) -> None:
    client = make_client(
        session,
        context=context_with_permissions("user:read"),
    )
    async with client:
        response = await client.get("/api/v1/users")

    assert response.status_code == 200
    assert response.json()["code"] == 0


async def test_me_permissions_returns_current_user_codes(
    session: AsyncSession,
) -> None:
    client = make_client(
        session,
        context=context_with_permissions("role:read", "user:read"),
    )
    async with client:
        response = await client.get("/api/v1/me/permissions")

    assert response.status_code == 200
    assert response.json()["data"] == ["role:read", "user:read"]


async def test_me_menus_returns_only_visible_tree(
    session: AsyncSession,
) -> None:
    role = await create_role(
        session,
        RoleCreate(code="menu-role", name="菜单角色"),
    )
    root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    menu = await create_menu(
        session,
        MenuCreate(
            parent_id=root.id,
            type=MenuType.MENU,
            name="用户管理",
            path="/system/users",
        ),
    )
    button = await create_menu(
        session,
        MenuCreate(
            parent_id=menu.id,
            type=MenuType.BUTTON,
            name="新增用户",
            permission_code="user:create",
        ),
    )
    disabled_root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="停用目录"),
    )
    disabled_menu = await create_menu(
        session,
        MenuCreate(
            parent_id=disabled_root.id,
            type=MenuType.MENU,
            name="停用菜单",
        ),
    )
    invisible_root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="隐藏目录", visible=False),
    )
    invisible_menu = await create_menu(
        session,
        MenuCreate(
            parent_id=invisible_root.id,
            type=MenuType.MENU,
            name="隐藏菜单",
        ),
    )
    await update_menu(
        session,
        disabled_root.id,
        MenuUpdate(status=MenuStatus.DISABLED),
    )
    await assign_role_menus(
        session,
        role.id,
        [
            root.id,
            menu.id,
            button.id,
            disabled_root.id,
            disabled_menu.id,
            invisible_root.id,
            invisible_menu.id,
        ],
    )
    client = make_client(
        session,
        context=context_with_permissions(
            "user:create",
            role_id=role.id,
        ),
    )

    async with client:
        response = await client.get("/api/v1/me/menus")

    assert response.status_code == 200
    tree = response.json()["data"]
    assert [node["id"] for node in tree] == [root.id]
    assert [node["id"] for node in tree[0]["children"]] == [menu.id]
    assert tree[0]["children"][0]["children"] == []


async def test_me_menus_disabled_role_returns_empty(
    session: AsyncSession,
) -> None:
    role = await create_role(
        session,
        RoleCreate(code="disabled-menu-role", name="停用菜单角色"),
    )
    root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    await assign_role_menus(session, role.id, [root.id])
    role = await update_role(
        session,
        role.id,
        RoleUpdate(status=RoleStatus.DISABLED),
        context=context_with_permissions("role:update", role_id=role.id),
    )
    client = make_client(
        session,
        context=context_with_permissions(role_id=role.id),
    )

    async with client:
        response = await client.get("/api/v1/me/menus")

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_me_menus_super_admin_returns_all_enabled_non_buttons(
    session: AsyncSession,
) -> None:
    role = await add_super_admin_role(session)
    root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    menu = await create_menu(
        session,
        MenuCreate(
            parent_id=root.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )
    await create_menu(
        session,
        MenuCreate(
            parent_id=menu.id,
            type=MenuType.BUTTON,
            name="新增用户",
        ),
    )
    disabled = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="停用目录"),
    )
    await update_menu(
        session,
        disabled.id,
        MenuUpdate(status=MenuStatus.DISABLED),
    )
    client = make_client(
        session,
        context=context_with_permissions(
            role_id=role.id,
            is_super_admin=True,
        ),
    )

    async with client:
        response = await client.get("/api/v1/me/menus")

    assert response.status_code == 200
    tree = response.json()["data"]
    assert [node["id"] for node in tree] == [root.id]
    assert [node["id"] for node in tree[0]["children"]] == [menu.id]
    assert tree[0]["children"][0]["children"] == []
