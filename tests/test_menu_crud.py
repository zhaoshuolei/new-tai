import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.menu import MenuStatus, MenuType
from app.repositories import menu as menu_repository
from app.schemas.menu import MenuCreate, MenuUpdate
from app.services.menu import (
    create_menu,
    delete_menu,
    get_menu_tree,
    list_enabled_permission_codes,
    update_menu,
)

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


async def test_create_dir_then_menu_then_button(session: AsyncSession) -> None:
    directory = await create_menu(
        session,
        MenuCreate(
            type=MenuType.DIR,
            name="系统管理",
            path="/system",
            icon="settings",
        ),
    )
    menu = await create_menu(
        session,
        MenuCreate(
            parent_id=directory.id,
            type=MenuType.MENU,
            name="用户管理",
            path="/system/users",
            component="system/users/index",
            permission_code="system:user:list",
        ),
    )
    button = await create_menu(
        session,
        MenuCreate(
            parent_id=menu.id,
            type=MenuType.BUTTON,
            name="新增用户",
            permission_code="system:user:create",
        ),
    )

    tree = await get_menu_tree(session)

    assert directory.parent_id is None
    assert menu.parent_id == directory.id
    assert button.parent_id == menu.id
    assert [node.id for node in tree] == [directory.id]
    assert [node.id for node in tree[0].children] == [menu.id]
    assert [node.id for node in tree[0].children[0].children] == [button.id]


async def test_button_under_button_returns_4001(session: AsyncSession) -> None:
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
        await create_menu(
            session,
            MenuCreate(
                parent_id=button.id,
                type=MenuType.BUTTON,
                name="查看用户",
            ),
        )

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY


async def test_button_under_dir_returns_4001(session: AsyncSession) -> None:
    directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )

    with pytest.raises(BusinessError) as error:
        await create_menu(
            session,
            MenuCreate(
                parent_id=directory.id,
                type=MenuType.BUTTON,
                name="新增用户",
            ),
        )

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY


async def test_duplicate_permission_code_returns_4002(
    session: AsyncSession,
) -> None:
    await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户管理",
            permission_code="system:user:list",
        ),
    )

    with pytest.raises(BusinessError) as error:
        await create_menu(
            session,
            MenuCreate(
                type=MenuType.MENU,
                name="角色管理",
                permission_code="system:user:list",
            ),
        )

    assert error.value.code == ErrorCode.PERMISSION_CODE_EXISTS


async def test_disabled_parent_hides_descendants_from_me_menus(
    session: AsyncSession,
) -> None:
    directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    await create_menu(
        session,
        MenuCreate(
            parent_id=directory.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )
    visible_directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="报表中心"),
    )
    await update_menu(
        session,
        directory.id,
        MenuUpdate(status=MenuStatus.DISABLED),
    )

    tree = await get_menu_tree(session)

    assert [node.id for node in tree] == [visible_directory.id]


async def test_list_enabled_permission_codes_excludes_disabled_and_nulls(
    session: AsyncSession,
) -> None:
    active = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户管理",
            permission_code="system:user:list",
        ),
    )
    disabled = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="角色管理",
            permission_code="system:role:list",
        ),
    )
    await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="无权限码菜单",
        ),
    )
    await update_menu(
        session,
        disabled.id,
        MenuUpdate(status=MenuStatus.DISABLED),
    )

    codes = await list_enabled_permission_codes(session)

    assert codes == frozenset({"system:user:list"})
    assert active.permission_code in codes


async def test_delete_menu_with_children_returns_4001(
    session: AsyncSession,
) -> None:
    parent = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    await create_menu(
        session,
        MenuCreate(
            parent_id=parent.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )

    with pytest.raises(BusinessError) as error:
        await delete_menu(session, parent.id)

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY


async def test_delete_leaf_menu_succeeds(session: AsyncSession) -> None:
    menu = await create_menu(
        session,
        MenuCreate(type=MenuType.MENU, name="用户管理"),
    )

    await delete_menu(session, menu.id)

    assert await menu_repository.get_by_id(session, menu.id) is None


async def test_update_menu_changes_fields_and_validates_new_parent(
    session: AsyncSession,
) -> None:
    first_directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    second_directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="报表中心"),
    )
    menu = await create_menu(
        session,
        MenuCreate(
            parent_id=first_directory.id,
            type=MenuType.MENU,
            name="用户管理",
            visible=False,
        ),
    )

    updated = await update_menu(
        session,
        menu.id,
        MenuUpdate(
            parent_id=second_directory.id,
            name="用户与权限",
            sort_order=20,
        ),
    )

    assert updated.parent_id == second_directory.id
    assert updated.name == "用户与权限"
    assert updated.sort_order == 20
    assert updated.visible is False


async def test_update_menu_to_non_menu_parent_returns_4001(
    session: AsyncSession,
) -> None:
    directory = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="系统管理"),
    )
    button = await create_menu(
        session,
        MenuCreate(
            parent_id=directory.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )
    leaf_button = await create_menu(
        session,
        MenuCreate(
            parent_id=button.id,
            type=MenuType.BUTTON,
            name="新增用户",
        ),
    )

    with pytest.raises(BusinessError) as error:
        await update_menu(
            session,
            leaf_button.id,
            MenuUpdate(parent_id=directory.id),
        )

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY


async def test_menu_endpoints_return_success_envelope(
    session: AsyncSession,
) -> None:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            create_response = await client.post(
                "/api/v1/menus",
                json={
                    "type": "DIR",
                    "name": "系统管理",
                    "path": "/system",
                },
            )
            menu_id = create_response.json()["data"]["id"]
            tree_response = await client.get("/api/v1/menus/tree")
            update_response = await client.patch(
                f"/api/v1/menus/{menu_id}",
                json={"name": "平台管理"},
            )
            delete_response = await client.delete(f"/api/v1/menus/{menu_id}")
    finally:
        application.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert create_response.json()["code"] == 0
    assert create_response.json()["data"]["type"] == "DIR"
    assert tree_response.status_code == 200
    assert tree_response.json()["data"][0]["name"] == "系统管理"
    assert update_response.status_code == 200
    assert update_response.json()["data"]["name"] == "平台管理"
    assert delete_response.status_code == 200
    assert delete_response.json()["message"] == "deleted"


def test_openapi_registers_menu_routes_under_api_v1() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/v1/menus/tree" in paths
    assert "/api/v1/menus" in paths
    assert "/api/v1/menus/{menu_id}" in paths
    assert "/menus/tree" not in paths
