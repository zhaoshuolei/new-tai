"""用户、角色、部门、菜单的软删除与恢复行为。"""

import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department
from app.models.menu import Menu, MenuType
from app.models.role import DataScope, Role
from app.models.user import User
from app.repositories import department as department_repository
from app.repositories import menu as menu_repository
from app.repositories import role as role_repository
from app.repositories import user as user_repository
from app.schemas.department import DepartmentCreate
from app.schemas.menu import MenuCreate
from app.schemas.role import RoleCreate
from app.schemas.user import UserCreate
from app.services.department import (
    create_department,
    delete_department,
    get_tree,
    restore_department,
)
from app.services.menu import create_menu, delete_menu, restore_menu
from app.services.role import create_role, delete_role, restore_role
from app.services.user import create_local_user, delete_user, restore_user
from tests.conftest import FakeRedis
from tests.customer_helpers import api_client, session  # noqa: F401

__all__ = ("session",)

CONTEXT = AuthContext(
    user_id=1,
    role_id=1,
    department_id=None,
    data_scope=DataScope.ALL,
    custom_department_ids=frozenset(),
    include_children=True,
    permission_codes=frozenset(
        {
            "user:delete",
            "role:delete",
            "dept:delete",
            "menu:delete",
        }
    ),
    is_super_admin=True,
)


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def test_deleted_user_is_retained_and_username_can_be_reused(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    username = unique("soft-user")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password1"),
    )

    await delete_user(session, user.id, context=CONTEXT, redis=fake_redis)

    retained = await session.get(User, user.id)
    assert retained is not None
    assert retained.deleted_at is not None
    assert retained.deleted_by == CONTEXT.user_id
    assert await user_repository.get_by_id(session, user.id) is None
    replacement = await create_local_user(
        session,
        UserCreate(username=username, password="password2"),
    )
    assert replacement.id != user.id

    with pytest.raises(BusinessError):
        await restore_user(session, user.id, context=CONTEXT, redis=fake_redis)


async def test_deleted_role_is_retained_and_code_can_be_reused(
    session: AsyncSession,
) -> None:
    code = unique("soft_role")
    role = await create_role(session, RoleCreate(code=code, name="软删除角色"))

    await delete_role(session, role.id, context=CONTEXT)

    retained = await session.get(Role, role.id)
    assert retained is not None
    assert retained.deleted_at is not None
    assert retained.deleted_by == CONTEXT.user_id
    assert await role_repository.get_by_id(session, role.id) is None
    replacement = await create_role(session, RoleCreate(code=code, name="替代角色"))
    assert replacement.id != role.id

    with pytest.raises(BusinessError):
        await restore_role(session, role.id, context=CONTEXT)


async def test_deleted_department_is_retained_and_restorable(
    session: AsyncSession,
) -> None:
    department = await create_department(
        session,
        DepartmentCreate(name=unique("软删除部门")),
    )

    await delete_department(session, department.id, context=CONTEXT)

    retained = await session.get(Department, department.id)
    assert retained is not None
    assert retained.deleted_at is not None
    assert retained.deleted_by == CONTEXT.user_id
    assert await department_repository.get_by_id(session, department.id) is None
    assert department.id not in [node.id for node in await get_tree(session)]

    restored = await restore_department(session, department.id, context=CONTEXT)
    assert restored.deleted_at is None
    assert restored.deleted_by is None


async def test_restore_menu_locks_target_and_parent_together(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name=unique("恢复父菜单")),
    )
    child = await create_menu(
        session,
        MenuCreate(
            parent_id=parent.id,
            type=MenuType.MENU,
            name=unique("恢复子菜单"),
        ),
    )
    await delete_menu(session, child.id, context=CONTEXT)
    original_lock = menu_repository.lock_menu_nodes
    locked_ids: set[int] = set()
    include_deleted_flags: list[bool] = []

    async def record_lock(
        lock_session: AsyncSession,
        menu_ids: set[int],
        *,
        include_deleted: bool = False,
    ) -> dict[int, Menu]:
        locked_ids.update(menu_ids)
        include_deleted_flags.append(include_deleted)
        return await original_lock(
            lock_session,
            menu_ids,
            include_deleted=include_deleted,
        )

    monkeypatch.setattr(menu_repository, "lock_menu_nodes", record_lock)

    await restore_menu(session, child.id, context=CONTEXT)

    assert locked_ids == {parent.id, child.id}
    assert include_deleted_flags == [True]


async def test_deleted_menu_is_retained_and_permission_can_be_reused(
    session: AsyncSession,
) -> None:
    suffix = "".join(chr(ord("a") + int(char, 16)) for char in uuid4().hex[:10])
    permission_code = f"soft_{suffix}:read"
    menu = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="软删除菜单",
            permission_code=permission_code,
        ),
    )

    await delete_menu(session, menu.id, context=CONTEXT)

    retained = await session.get(Menu, menu.id)
    assert retained is not None
    assert retained.deleted_at is not None
    assert retained.deleted_by == CONTEXT.user_id
    assert await menu_repository.get_by_id(session, menu.id) is None
    replacement = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="替代菜单",
            permission_code=permission_code,
        ),
    )
    assert replacement.id != menu.id

    with pytest.raises(BusinessError):
        await restore_menu(session, menu.id, context=CONTEXT)


async def test_restore_user_maps_duplicate_index_without_requery(
    session: AsyncSession,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique("restore-race"), password="password1"),
    )
    await delete_user(session, user.id, context=CONTEXT, redis=fake_redis)

    async def duplicate(
        _session: AsyncSession,
        _user: User,
    ) -> None:
        raise sa.exc.IntegrityError(
            "UPDATE",
            {},
            Exception(
                1062,
                "Duplicate entry 'restore-race' for key 'user.ix_user_active_username'",
            ),
        )

    monkeypatch.setattr(user_repository, "restore", duplicate)

    with pytest.raises(BusinessError) as exc:
        await restore_user(session, user.id, context=CONTEXT, redis=fake_redis)

    assert exc.value.code == ErrorCode.USERNAME_EXISTS


async def test_downgrade_guard_rejects_soft_deleted_rows(
    session: AsyncSession,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.d7c1e4a9b2f6_system_soft_delete"
    )
    table_name = f"soft_delete_guard_{uuid4().hex[:12]}"
    await session.execute(
        sa.text(
            f"CREATE TEMPORARY TABLE `{table_name}` ("
            "id BIGINT PRIMARY KEY, deleted_at DATETIME(6) NULL)"
        )
    )
    await session.execute(
        sa.text(
            f"INSERT INTO `{table_name}` (id, deleted_at) "
            "VALUES (1, NULL), (2, UTC_TIMESTAMP(6))"
        )
    )

    with pytest.raises(RuntimeError, match=table_name):
        await session.run_sync(
            lambda sync_session: migration._ensure_no_soft_deleted_rows(
                sync_session.connection(),
                table_names=(table_name,),
            )
        )


async def test_restore_user_rejects_deleted_role_or_department(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    role = await create_role(
        session,
        RoleCreate(code=unique("restore_role"), name="待删除角色"),
    )
    department = await create_department(
        session,
        DepartmentCreate(name=unique("待删除部门")),
    )
    user = await create_local_user(
        session,
        UserCreate(username=unique("restore-user"), password="password1"),
    )
    user.role_id = role.id
    user.department_id = department.id
    await session.flush()

    await delete_user(session, user.id, context=CONTEXT, redis=fake_redis)
    await delete_role(session, role.id, context=CONTEXT)
    await delete_department(session, department.id, context=CONTEXT)

    with pytest.raises(BusinessError) as exc:
        await restore_user(session, user.id, context=CONTEXT, redis=fake_redis)
    assert exc.value.code == ErrorCode.CONFLICT


async def test_recycle_bin_and_restore_routes(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique("api-user"), password="password1"),
    )
    role = await create_role(
        session,
        RoleCreate(code=unique("api_role"), name="API 角色"),
    )
    department = await create_department(
        session,
        DepartmentCreate(name=unique("API 部门")),
    )
    menu = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name=unique("API 菜单")),
    )
    await delete_user(session, user.id, context=CONTEXT, redis=fake_redis)
    await delete_role(session, role.id, context=CONTEXT)
    await delete_department(session, department.id, context=CONTEXT)
    await delete_menu(session, menu.id, context=CONTEXT)

    async with api_client(session, CONTEXT, fake_redis) as client:
        users = await client.get("/api/v1/users/recycle-bin")
        roles = await client.get("/api/v1/roles/recycle-bin")
        departments = await client.get("/api/v1/departments/recycle-bin")
        menus = await client.get("/api/v1/menus/recycle-bin")

        assert users.status_code == 200
        assert [item["id"] for item in users.json()["data"]["items"]] == [user.id]
        assert roles.status_code == 200
        assert [item["id"] for item in roles.json()["data"]] == [role.id]
        assert departments.status_code == 200
        assert [item["id"] for item in departments.json()["data"]] == [department.id]
        assert menus.status_code == 200
        assert [item["id"] for item in menus.json()["data"]] == [menu.id]

        restored_user = await client.post(f"/api/v1/users/{user.id}/restore")
        restored_role = await client.post(f"/api/v1/roles/{role.id}/restore")
        restored_department = await client.post(
            f"/api/v1/departments/{department.id}/restore"
        )
        restored_menu = await client.post(f"/api/v1/menus/{menu.id}/restore")

        assert restored_user.status_code == 200
        assert restored_role.status_code == 200
        assert restored_department.status_code == 200
        assert restored_menu.status_code == 200
