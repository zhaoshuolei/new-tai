import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.security import verify_password
from app.models.department import Department, DepartmentStatus
from app.models.menu import Menu, MenuStatus, MenuType
from app.models.role import DataScope, Role, RoleMenu, RoleStatus
from app.models.user import User, UserStatus
from app.services import auth as auth_service
from app.services.menu import _is_valid_parent_type
from scripts.init_data import init_data

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

EXPECTED_PERMISSION_CODES = {
    "user:create",
    "user:read",
    "user:update",
    "user:delete",
    "user:assign",
    "role:create",
    "role:read",
    "role:update",
    "role:delete",
    "role:assign",
    "menu:create",
    "menu:read",
    "menu:update",
    "menu:delete",
    "dept:create",
    "dept:read",
    "dept:update",
    "dept:delete",
    "audit:read",
    "login_log:read",
    "monitor:read",
    "exception:read",
    "exception:update",
    "customer:read",
    "customer:create",
    "customer:update",
    "customer:delete",
    "integration:read",
    "integration:update",
    "integration_feishu:read",
    "integration_feishu:update",
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


async def _count(session: AsyncSession, model: Any) -> int:
    value = await session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


async def test_init_creates_pending_department_as_system_root(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    department = await session.scalar(
        select(Department).where(
            Department.is_system.is_(True),
            Department.name == "待分配",
        )
    )

    assert department is not None
    assert department.parent_id is None
    assert department.path == f"/{department.id}/"
    assert department.status == DepartmentStatus.ACTIVE


async def test_init_creates_super_admin_and_default_roles(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    super_admin = await session.scalar(select(Role).where(Role.code == "super_admin"))
    default = await session.scalar(select(Role).where(Role.code == "default"))

    assert super_admin is not None
    assert super_admin.is_system is True
    assert super_admin.status == RoleStatus.ACTIVE
    assert super_admin.data_scope == DataScope.ALL
    assert super_admin.data_scope_include_children is True
    assert default is not None
    assert default.is_system is True
    assert default.status == RoleStatus.ACTIVE
    assert await _count(session, RoleMenu) == 1
    assert (
        await session.scalar(
            select(RoleMenu.c.menu_id).where(RoleMenu.c.role_id == super_admin.id)
        )
        is None
    )


async def test_default_role_is_scoped_to_self(session: AsyncSession) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    default = await session.scalar(select(Role).where(Role.code == "default"))
    profile = await session.scalar(
        select(Menu).where(Menu.name == "个人资料", Menu.parent_id.is_(None))
    )

    assert default is not None
    assert default.data_scope == DataScope.SELF
    assert profile is not None
    assert (
        await session.scalar(
            select(RoleMenu.c.menu_id).where(RoleMenu.c.role_id == default.id)
        )
        == profile.id
    )


async def test_init_creates_baseline_menu_tree(session: AsyncSession) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    menus = list((await session.scalars(select(Menu))).all())
    menus_by_code = {
        menu.permission_code: menu for menu in menus if menu.permission_code is not None
    }
    menus_by_id = {menu.id: menu for menu in menus}
    for menu in menus:
        parent = menus_by_id.get(menu.parent_id) if menu.parent_id is not None else None
        assert _is_valid_parent_type(
            menu.type,
            parent.type if parent is not None else None,
        ), f"invalid parent for seed menu: {menu.name}"
    root = await session.scalar(
        select(Menu).where(
            Menu.type == MenuType.DIR,
            Menu.name == "系统管理",
            Menu.parent_id.is_(None),
        )
    )
    profile = await session.scalar(
        select(Menu).where(
            Menu.type == MenuType.MENU,
            Menu.name == "个人资料",
            Menu.parent_id.is_(None),
        )
    )

    assert root is not None
    assert profile is not None
    assert profile.permission_code is None
    assert set(menus_by_code) == EXPECTED_PERMISSION_CODES
    assert all(menu.status == MenuStatus.ACTIVE for menu in menus)

    for resource in ("用户管理", "角色管理", "菜单管理", "部门管理"):
        resource_menu = await session.scalar(
            select(Menu).where(
                Menu.parent_id == root.id,
                Menu.name == resource,
            )
        )
        assert resource_menu is not None
        assert resource_menu.type == MenuType.MENU

    for code, menu in menus_by_code.items():
        action = code.split(":", 1)[1]
        if action == "read":
            assert menu.type == MenuType.MENU
        else:
            assert menu.type == MenuType.BUTTON
            assert menu.parent_id is not None

    for module, resource in (
        ("user", "用户管理"),
        ("role", "角色管理"),
        ("menu", "菜单管理"),
        ("dept", "部门管理"),
    ):
        module_menu = await session.scalar(
            select(Menu).where(
                Menu.parent_id == root.id,
                Menu.name == resource,
            )
        )
        assert module_menu is not None
        button_parent_ids = {
            menu.parent_id
            for code, menu in menus_by_code.items()
            if code.startswith(f"{module}:") and not code.endswith(":read")
        }
        assert button_parent_ids == {module_menu.id}

    logs_directory = await session.scalar(
        select(Menu).where(Menu.path == "/system/logs")
    )
    monitor_menu = menus_by_code["monitor:read"]
    exceptions_menu = menus_by_code["exception:read"]
    exceptions_ack = menus_by_code["exception:update"]

    assert logs_directory is not None
    assert logs_directory.type == MenuType.DIR
    assert logs_directory.parent_id == root.id
    assert menus_by_code["audit:read"].parent_id == logs_directory.id
    assert menus_by_code["login_log:read"].parent_id == logs_directory.id
    assert monitor_menu.parent_id == root.id
    assert exceptions_menu.parent_id == root.id
    assert exceptions_ack.type == MenuType.BUTTON
    assert exceptions_ack.parent_id == exceptions_menu.id


async def test_integration_update_button_is_under_integration_menu(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    update_button = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration:update")
    )
    integration_menu = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration:read")
    )

    assert update_button is not None
    assert integration_menu is not None
    assert update_button.type == MenuType.BUTTON
    assert update_button.parent_id == integration_menu.id


async def test_feishu_integration_menu_and_button_are_seeded(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    menu = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration_feishu:read")
    )
    button = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration_feishu:update")
    )

    assert menu is not None
    assert menu.type == MenuType.MENU
    assert menu.path == "/system/integrations/feishu"
    assert button is not None
    assert button.type == MenuType.BUTTON
    assert button.parent_id == menu.id


async def test_customer_buttons_are_under_customer_list(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    customer_list = await session.scalar(
        select(Menu).where(Menu.permission_code == "customer:read")
    )

    assert customer_list is not None
    assert customer_list.type == MenuType.MENU

    for code in ("customer:create", "customer:update", "customer:delete"):
        button = await session.scalar(
            select(Menu).where(Menu.permission_code == code)
        )
        assert button is not None
        assert button.type == MenuType.BUTTON
        assert button.parent_id == customer_list.id


async def test_default_role_has_no_customer_permissions(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    default = await session.scalar(select(Role).where(Role.code == "default"))
    assert default is not None

    assigned_codes = set(
        (
            await session.scalars(
                select(Menu.permission_code)
                .join(RoleMenu, RoleMenu.c.menu_id == Menu.id)
                .where(RoleMenu.c.role_id == default.id)
            )
        ).all()
    )

    assert assigned_codes.isdisjoint(
        {
            "customer:read",
            "customer:create",
            "customer:update",
            "customer:delete",
            "integration:read",
            "integration:update",
            "integration_feishu:read",
            "integration_feishu:update",
        }
    )


async def test_init_creates_local_emergency_superadmin(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="emergency-admin",
        superadmin_password="secret-password1",
    )

    user = await session.scalar(select(User).where(User.username == "emergency-admin"))
    super_admin = await session.scalar(select(Role).where(Role.code == "super_admin"))

    assert user is not None
    assert super_admin is not None
    assert user.feishu_open_id is None
    assert user.password_hash is not None
    assert user.password_hash != "secret-password1"
    assert verify_password("secret-password1", user.password_hash)
    assert user.role_id == super_admin.id
    assert user.status == UserStatus.ACTIVE


async def test_init_is_idempotent_when_run_twice(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )
    department = await session.scalar(
        select(Department).where(Department.name == "待分配")
    )
    super_admin = await session.scalar(select(Role).where(Role.code == "super_admin"))
    user = await session.scalar(select(User).where(User.username == "superadmin"))
    assert department is not None
    assert super_admin is not None
    assert user is not None

    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    assert await _count(session, Department) == 1
    assert await _count(session, Role) == 2
    assert await _count(session, Menu) == 35
    assert await _count(session, User) == 1
    assert await _count(session, RoleMenu) == 1
    assert (
        await session.scalar(select(Department.id).where(Department.name == "待分配"))
        == department.id
    )
    assert (
        await session.scalar(select(Role.id).where(Role.code == "super_admin"))
        == super_admin.id
    )
    assert (
        await session.scalar(select(User.id).where(User.username == "superadmin"))
        == user.id
    )


async def test_init_requires_superadmin_password_from_env(
    session: AsyncSession,
) -> None:
    counts_before = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )

    with pytest.raises(ValueError, match="superadmin password"):
        await init_data(
            session,
            superadmin_username="superadmin",
            superadmin_password="",
        )

    counts_after = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )
    assert counts_after == counts_before


async def test_init_rejects_superadmin_password_below_policy(
    session: AsyncSession,
) -> None:
    counts_before = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )

    with pytest.raises(ValueError, match="8"):
        await init_data(
            session,
            superadmin_username="superadmin",
            superadmin_password="abc1234",
        )

    counts_after = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )
    assert counts_after == counts_before


@pytest.mark.parametrize("invalid_username", ["   ", " a ", "a" * 65])
async def test_init_rejects_superadmin_username_invalid_after_normalization(
    session: AsyncSession,
    invalid_username: str,
) -> None:
    counts_before = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )

    with pytest.raises(ValueError, match="username"):
        await init_data(
            session,
            superadmin_username=invalid_username,
            superadmin_password="abc12345",
        )

    counts_after = (
        await _count(session, Department),
        await _count(session, Role),
        await _count(session, Menu),
        await _count(session, User),
    )
    assert counts_after == counts_before


async def test_init_normalizes_superadmin_username(
    session: AsyncSession,
) -> None:
    password = "abc12345"
    await init_data(
        session,
        superadmin_username="  EmergencyAdmin  ",
        superadmin_password=password,
    )

    user = await session.scalar(select(User))
    authenticated = await auth_service.authenticate_local_user(
        session,
        "EMERGENCYADMIN",
        password,
    )

    assert user is not None
    assert user.username == "emergencyadmin"
    assert authenticated.id == user.id
