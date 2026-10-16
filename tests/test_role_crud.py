import os
from collections.abc import AsyncIterator, Iterable
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.menu import MenuStatus, MenuType
from app.models.role import (
    DataScope,
    Role,
    RoleMenu,
    RoleStatus,
)
from app.models.role_department import RoleDepartment
from app.repositories import role as role_repository
from app.schemas.menu import MenuCreate, MenuUpdate
from app.schemas.role import RoleCreate, RoleDataScopeUpdate, RoleUpdate
from app.services.menu import create_menu, update_menu
from app.services.role import (
    assign_role_menus,
    create_role,
    delete_role,
    get_role,
    list_role_permission_codes,
    list_roles,
    set_role_data_scope,
    update_role,
)

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

ROLE_PERMISSION_CONTEXT = AuthContext(
    user_id=1,
    role_id=1,
    department_id=None,
    data_scope=DataScope.ALL,
    custom_department_ids=frozenset(),
    include_children=True,
    permission_codes=frozenset(
        {"role:create", "role:read", "role:update", "role:delete", "role:assign"}
    ),
    is_super_admin=False,
)

SUPER_ADMIN_CONTEXT = AuthContext(
    user_id=1,
    role_id=1,
    department_id=None,
    data_scope=DataScope.ALL,
    custom_department_ids=frozenset(),
    include_children=True,
    permission_codes=frozenset(
        {"role:create", "role:read", "role:update", "role:delete", "role:assign"}
    ),
    is_super_admin=True,
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


class _RecordingScalarSession:
    statement: Any = None

    async def scalar(self, statement: Any) -> None:
        self.statement = statement
        return None


async def add_role(
    session: AsyncSession,
    *,
    code: str,
    name: str | None = None,
    data_scope: DataScope = DataScope.SELF,
    is_system: bool = False,
    status: RoleStatus = RoleStatus.ACTIVE,
) -> Role:
    role = Role(
        code=code,
        name=name or code,
        description=None,
        data_scope=data_scope,
        data_scope_include_children=True,
        is_system=is_system,
        status=status,
    )
    session.add(role)
    await session.commit()
    return role


async def add_system_department(
    session: AsyncSession,
    *,
    name: str = "待分配",
) -> Department:
    department = Department(
        name=name,
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.commit()
    return department


async def test_create_and_get_role(session: AsyncSession) -> None:
    role = await create_role(
        session,
        RoleCreate(
            code="operator",
            name="运营",
            description="运营人员",
            data_scope=DataScope.DEPT,
        ),
    )

    loaded = await get_role(session, role.id)

    assert loaded.id == role.id
    assert loaded.code == "operator"
    assert loaded.name == "运营"
    assert loaded.description == "运营人员"
    assert loaded.data_scope == DataScope.DEPT
    assert loaded.data_scope_include_children is True
    assert loaded.is_system is False
    assert loaded.status == RoleStatus.ACTIVE
    assert await list_roles(session) == [role]


async def test_duplicate_role_code_conflicts(session: AsyncSession) -> None:
    await create_role(session, RoleCreate(code="operator", name="运营"))

    with pytest.raises(BusinessError) as error:
        await create_role(session, RoleCreate(code="operator", name="另一角色"))

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409


@pytest.mark.parametrize("code", ["super_admin", "default"])
async def test_create_role_rejects_reserved_system_code(
    session: AsyncSession,
    code: str,
) -> None:
    with pytest.raises(BusinessError) as error:
        await create_role(session, RoleCreate(code=code, name="保留角色"))

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    assert await role_repository.get_by_code(session, code) is None


@pytest.mark.parametrize("code", ["super_admin", "default"])
async def test_update_role_rejects_reserved_system_code(
    session: AsyncSession,
    code: str,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))

    with pytest.raises(BusinessError) as error:
        await update_role(
            session,
            role.id,
            RoleUpdate(code=code),
            context=ROLE_PERMISSION_CONTEXT,
        )

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    loaded = await get_role(session, role.id)
    assert loaded.code == "operator"


async def test_system_role_cannot_be_deleted(session: AsyncSession) -> None:
    role = await add_role(
        session,
        code="super_admin",
        data_scope=DataScope.ALL,
        is_system=True,
    )

    with pytest.raises(BusinessError) as error:
        await delete_role(
            session,
            role.id,
            context=SUPER_ADMIN_CONTEXT,
        )

    assert error.value.code == ErrorCode.SYSTEM_ROLE_IMMUTABLE
    assert await role_repository.get_by_id(session, role.id) is not None


async def test_delete_role_in_use_returns_3002(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await add_role(session, code="operator")

    async def has_users(
        _session: AsyncSession,
        role_id: int,
    ) -> bool:
        return role_id == role.id

    monkeypatch.setattr(role_repository, "has_users", has_users)

    with pytest.raises(BusinessError) as error:
        await delete_role(
            session,
            role.id,
            context=ROLE_PERMISSION_CONTEXT,
        )

    assert error.value.code == ErrorCode.ROLE_IN_USE
    assert await role_repository.get_by_id(session, role.id) is not None


async def test_assign_menus_replaces_previous_set(session: AsyncSession) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    user_read = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )
    user_update = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户更新",
            permission_code="user:update",
        ),
    )

    await assign_role_menus(session, role.id, [user_read.id, user_update.id])
    assert await list_role_permission_codes(session, role) == frozenset(
        {"user:read", "user:update"}
    )

    await assign_role_menus(session, role.id, [user_update.id])
    assert await list_role_permission_codes(session, role) == frozenset({"user:update"})

    result = await session.scalars(
        select(RoleMenu.c.menu_id).where(RoleMenu.c.role_id == role.id)
    )
    assert set(result.all()) == {user_update.id}


@pytest.mark.asyncio
async def test_lock_role_by_id_uses_for_update_query() -> None:
    recording = _RecordingScalarSession()

    await role_repository.lock_by_id(
        cast(AsyncSession, recording),
        10,
    )

    compiled = str(
        recording.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "`role`.id = 10" in compiled
    assert "FOR UPDATE" in compiled


async def test_assign_menus_locks_role_before_replacement(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    original_lock = role_repository.lock_by_id
    original_replace = role_repository.replace_role_menus
    events: list[str] = []

    async def record_lock(
        lock_session: AsyncSession,
        role_id: int,
    ) -> Role | None:
        events.append("lock")
        return await original_lock(lock_session, role_id)

    async def record_replace(
        replace_session: AsyncSession,
        role_id: int,
        menu_ids: set[int],
    ) -> None:
        events.append("replace")
        await original_replace(replace_session, role_id, menu_ids)

    monkeypatch.setattr(role_repository, "lock_by_id", record_lock)
    monkeypatch.setattr(role_repository, "replace_role_menus", record_replace)

    await assign_role_menus(session, role.id, [])

    assert events == ["lock", "replace"]


async def test_assign_menus_maps_deadlock_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))

    async def deadlock(
        _session: AsyncSession,
        _role_id: int,
    ) -> Role | None:
        raise OperationalError(
            "SELECT",
            {},
            Exception(1213, "Deadlock found when trying to get lock"),
        )

    monkeypatch.setattr(role_repository, "lock_by_id", deadlock)

    with pytest.raises(BusinessError) as error:
        await assign_role_menus(session, role.id, [])

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    assert "稍后重试" in error.value.message


async def test_assign_menus_maps_duplicate_key_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))

    async def duplicate(
        _session: AsyncSession,
        _role_id: int,
        _menu_ids: set[int],
    ) -> None:
        raise IntegrityError(
            "INSERT",
            {},
            Exception(1062, "Duplicate entry '1-2' for key 'role_menu.PRIMARY'"),
        )

    monkeypatch.setattr(role_repository, "replace_role_menus", duplicate)

    with pytest.raises(BusinessError) as error:
        await assign_role_menus(session, role.id, [])

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    assert "稍后重试" in error.value.message


async def test_disabled_role_yields_empty_permission_codes(
    session: AsyncSession,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    menu = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )
    await assign_role_menus(session, role.id, [menu.id])
    role = await update_role(
        session,
        role.id,
        RoleUpdate(status=RoleStatus.DISABLED),
        context=ROLE_PERMISSION_CONTEXT,
    )

    assert await list_role_permission_codes(session, role) == frozenset()


async def test_super_admin_permissions_come_from_all_enabled_menus(
    session: AsyncSession,
) -> None:
    role = await add_role(
        session,
        code="super_admin",
        data_scope=DataScope.ALL,
        is_system=True,
    )
    active = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )
    disabled = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户更新",
            permission_code="user:update",
        ),
    )
    await update_menu(
        session,
        disabled.id,
        MenuUpdate(status=MenuStatus.DISABLED),
    )

    assert await list_role_permission_codes(session, role) == frozenset({"user:read"})
    assert active.permission_code == "user:read"


async def test_newly_added_menu_is_automatically_in_super_admin(
    session: AsyncSession,
) -> None:
    role = await add_role(
        session,
        code="super_admin",
        data_scope=DataScope.ALL,
        is_system=True,
    )
    await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )
    assert await list_role_permission_codes(session, role) == frozenset({"user:read"})

    await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户更新",
            permission_code="user:update",
        ),
    )

    assert await list_role_permission_codes(session, role) == frozenset(
        {"user:read", "user:update"}
    )


async def test_super_admin_menu_assignment_does_not_store_rows(
    session: AsyncSession,
) -> None:
    role = await add_role(
        session,
        code="super_admin",
        data_scope=DataScope.ALL,
        is_system=True,
    )
    menu = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )

    await assign_role_menus(session, role.id, [menu.id])

    result = await session.scalars(
        select(RoleMenu.c.menu_id).where(RoleMenu.c.role_id == role.id)
    )
    assert result.all() == []


async def test_set_custom_data_scope_persists_valid_departments(
    session: AsyncSession,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    first = Department(
        parent_id=None,
        name="研发部",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    second = Department(
        parent_id=None,
        name="财务部",
        path="",
        sort_order=1,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    session.add_all([first, second])
    await session.flush()
    first.path = f"/{first.id}/"
    second.path = f"/{second.id}/"
    await session.commit()

    updated = await set_role_data_scope(
        session,
        role.id,
        RoleDataScopeUpdate(
            data_scope=DataScope.CUSTOM,
            data_scope_include_children=False,
            department_ids=[second.id, first.id],
        ),
    )

    assert updated.data_scope == DataScope.CUSTOM
    assert updated.data_scope_include_children is False
    result = await session.scalars(
        select(RoleDepartment.c.department_id).where(
            RoleDepartment.c.role_id == role.id
        )
    )
    assert set(result.all()) == {first.id, second.id}


async def test_set_data_scope_locks_role_before_replacement(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    original_lock = role_repository.lock_by_id
    original_replace = role_repository.replace_role_departments
    events: list[str] = []

    async def record_lock(
        lock_session: AsyncSession,
        role_id: int,
    ) -> Role | None:
        events.append("lock")
        return await original_lock(lock_session, role_id)

    async def record_replace(
        replace_session: AsyncSession,
        role_id: int,
        department_ids: Iterable[int],
    ) -> None:
        events.append("replace")
        await original_replace(replace_session, role_id, department_ids)

    monkeypatch.setattr(role_repository, "lock_by_id", record_lock)
    monkeypatch.setattr(
        role_repository,
        "replace_role_departments",
        record_replace,
    )

    await set_role_data_scope(
        session,
        role.id,
        RoleDataScopeUpdate(data_scope=DataScope.SELF),
    )

    assert events == ["lock", "replace"]


async def test_set_data_scope_maps_lock_timeout_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))

    async def lock_timeout(
        _session: AsyncSession,
        _role_id: int,
    ) -> Role | None:
        raise OperationalError(
            "SELECT",
            {},
            Exception(1205, "Lock wait timeout exceeded"),
        )

    monkeypatch.setattr(role_repository, "lock_by_id", lock_timeout)

    with pytest.raises(BusinessError) as error:
        await set_role_data_scope(
            session,
            role.id,
            RoleDataScopeUpdate(data_scope=DataScope.SELF),
        )

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    assert "稍后重试" in error.value.message


async def test_custom_data_scope_rejects_system_department(
    session: AsyncSession,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    department = await add_system_department(session)

    with pytest.raises(BusinessError) as error:
        await set_role_data_scope(
            session,
            role.id,
            RoleDataScopeUpdate(
                data_scope=DataScope.CUSTOM,
                department_ids=[department.id],
            ),
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER


async def test_non_custom_scope_clears_custom_departments(
    session: AsyncSession,
) -> None:
    role = await create_role(session, RoleCreate(code="operator", name="运营"))
    department = Department(
        parent_id=None,
        name="研发部",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.commit()
    await set_role_data_scope(
        session,
        role.id,
        RoleDataScopeUpdate(
            data_scope=DataScope.CUSTOM,
            department_ids=[department.id],
        ),
    )

    updated = await set_role_data_scope(
        session,
        role.id,
        RoleDataScopeUpdate(
            data_scope=DataScope.SELF,
            department_ids=[department.id],
        ),
    )

    assert updated.data_scope == DataScope.SELF
    result = await session.scalars(
        select(RoleDepartment.c.department_id).where(
            RoleDepartment.c.role_id == role.id
        )
    )
    assert result.all() == []


async def test_role_api_crud_and_assignments(
    session: AsyncSession,
    fake_redis: Any,
) -> None:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return ROLE_PERMISSION_CONTEXT

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_redis] = lambda: fake_redis
    menu = await create_menu(
        session,
        MenuCreate(
            type=MenuType.MENU,
            name="用户查看",
            permission_code="user:read",
        ),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/roles",
                json={"code": "operator", "name": "运营"},
            )
            role_id = created.json()["data"]["id"]
            listed = await client.get("/api/v1/roles")
            loaded = await client.get(f"/api/v1/roles/{role_id}")
            updated = await client.patch(
                f"/api/v1/roles/{role_id}",
                json={"name": "平台运营"},
            )
            assigned = await client.put(
                f"/api/v1/roles/{role_id}/menus",
                json={"menu_ids": [menu.id]},
            )
            scoped = await client.put(
                f"/api/v1/roles/{role_id}/data-scope",
                json={
                    "data_scope": "SELF",
                    "data_scope_include_children": True,
                    "department_ids": [],
                },
            )
            deleted = await client.delete(f"/api/v1/roles/{role_id}")
    finally:
        application.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["data"]["code"] == "operator"
    assert [item["id"] for item in listed.json()["data"]] == [role_id]
    assert loaded.json()["data"]["id"] == role_id
    assert updated.json()["data"]["name"] == "平台运营"
    assert assigned.status_code == 200
    assert scoped.json()["data"]["data_scope"] == "SELF"
    assert deleted.status_code == 200
    assert await role_repository.get_by_id(session, role_id) is None
