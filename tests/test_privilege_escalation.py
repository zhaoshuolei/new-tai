import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis, key
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope, Role, RoleStatus
from app.models.user import User, UserStatus
from app.schemas.role import RoleUpdate
from app.schemas.user import (
    UserCreate,
    UserDepartmentUpdate,
    UserRoleUpdate,
    UserStatusUpdate,
)
from app.services import auth as auth_service
from app.services.authorization import assert_target_visible, assignable_role_ids
from app.services.role import delete_role, update_role
from app.services.user import (
    assign_user_department,
    assign_user_role,
    create_local_user,
    delete_user,
    update_user_status,
)

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

ALL_USER_PERMISSIONS = frozenset(
    {"user:read", "user:update", "user:delete", "user:assign"}
)


class FailingRedis:
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
    ) -> bool:
        return True

    async def delete(self, *names: str) -> int:
        raise RuntimeError("redis unavailable")


class InspectingFailingRedis(FailingRedis):
    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self.session = session
        self.user_id = user_id
        self.observed: tuple[int | None, int] | None = None

    async def delete(self, *names: str) -> int:
        row = (
            await self.session.execute(
                select(User.role_id, User.token_version).where(User.id == self.user_id)
            )
        ).one()
        self.observed = (row.role_id, row.token_version)
        return await super().delete(*names)


class PostCommitDeleteFailingRedis:
    def __init__(self, snapshot_key: str, stale_snapshot: str) -> None:
        self.snapshot_key = snapshot_key
        self.stale_snapshot = stale_snapshot
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.snapshot_deletes = 0

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        get: bool = False,
    ) -> str | bool | None:
        existing = self.values.get(name)
        if nx and existing is not None:
            return None if get else False
        if xx and existing is None:
            return None if get else False
        self.values[name] = value
        if ex is not None:
            self.expirations[name] = ex
        return existing if get else True

    async def delete(self, *names: str) -> int:
        if self.snapshot_key in names:
            self.snapshot_deletes += 1
            if self.snapshot_deletes == 2:
                self.values[self.snapshot_key] = self.stale_snapshot
                raise RuntimeError("redis unavailable after commit")
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
                self.expirations.pop(name, None)
        if self.snapshot_key in names and self.snapshot_deletes == 1:
            self.values[self.snapshot_key] = self.stale_snapshot
            self.expirations[self.snapshot_key] = auth_service.AUTH_SNAPSHOT_TTL_SECONDS
        return deleted

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for name in tuple(self.values):
            if name.startswith(prefix):
                yield name


class RaceRepopulatingRedis:
    def __init__(self, snapshot_key: str, stale_snapshot: str) -> None:
        self.snapshot_key = snapshot_key
        self.stale_snapshot = stale_snapshot
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.repopulated = False

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        get: bool = False,
    ) -> str | bool | None:
        existing = self.values.get(name)
        if nx and existing is not None:
            return None if get else False
        if xx and existing is None:
            return None if get else False
        self.values[name] = value
        if ex is not None:
            self.expirations[name] = ex
        return existing if get else True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
                self.expirations.pop(name, None)
        if self.snapshot_key in names and not self.repopulated:
            self.values[self.snapshot_key] = self.stale_snapshot
            self.expirations[self.snapshot_key] = auth_service.AUTH_SNAPSHOT_TTL_SECONDS
            self.repopulated = True
        return deleted

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for name in tuple(self.values):
            if name.startswith(prefix):
                yield name


class RecordingRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        get: bool = False,
    ) -> str | bool | None:
        existing = self.values.get(name)
        if nx and existing is not None:
            return None if get else False
        if xx and existing is None:
            return None if get else False
        self.values[name] = value
        return existing if get else True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for name in tuple(self.values):
            if name.startswith(prefix):
                yield name


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


@pytest_asyncio.fixture
async def redis() -> RecordingRedis:
    return RecordingRedis()


def unique_code(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def add_role(
    session: AsyncSession,
    *,
    code: str | None = None,
    is_system: bool = False,
    status: RoleStatus = RoleStatus.ACTIVE,
) -> Role:
    resolved_code = code or unique_code("role")
    role = Role(
        code=resolved_code,
        name=resolved_code,
        description=None,
        data_scope=DataScope.ALL if resolved_code == "super_admin" else DataScope.SELF,
        data_scope_include_children=True,
        is_system=is_system,
        status=status,
    )
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return role


async def add_user(
    session: AsyncSession,
    *,
    role: Role | None = None,
    department: Department | None = None,
    status: UserStatus = UserStatus.ACTIVE,
    feishu: bool = False,
) -> User:
    user = User(
        feishu_open_id=f"ou-{uuid4().hex}" if feishu else None,
        username=None if feishu else unique_code("user"),
        password_hash=None,
        nickname=None,
        email=None,
        avatar=None,
        department_id=department.id if department is not None else None,
        role_id=role.id if role is not None else None,
        status=status,
        token_version=0,
        last_login_at=None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def add_department(
    session: AsyncSession,
    *,
    status: DepartmentStatus = DepartmentStatus.ACTIVE,
) -> Department:
    department = Department(
        parent_id=None,
        name=unique_code("department"),
        path="",
        sort_order=0,
        status=status,
        is_system=False,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.commit()
    await session.refresh(department)
    return department


def context_for(
    user: User,
    role: Role | None,
    *,
    is_super_admin: bool = False,
    data_scope: DataScope = DataScope.ALL,
    custom_department_ids: frozenset[int] = frozenset(),
    include_children: bool = True,
) -> AuthContext:
    return AuthContext(
        user_id=user.id,
        role_id=role.id if role is not None else None,
        department_id=user.department_id,
        data_scope=data_scope,
        custom_department_ids=custom_department_ids,
        include_children=include_children,
        permission_codes=ALL_USER_PERMISSIONS,
        is_super_admin=is_super_admin,
    )


async def seed_session_state(redis: RecordingRedis, user_id: int) -> None:
    await redis.set(key("auth", "snapshot", str(user_id)), "snapshot")
    await redis.set(key("auth", "refresh", str(user_id), "device-a"), "refresh-a")
    await redis.set(key("auth", "refresh", str(user_id), "device-b"), "refresh-b")


def assert_session_state_cleared(redis: RecordingRedis, user_id: int) -> None:
    assert key("auth", "snapshot", str(user_id)) not in redis.values
    prefix = key("auth", "refresh", str(user_id), "")
    assert not any(name.startswith(prefix) for name in redis.values)


async def test_non_super_admin_cannot_grant_super_admin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    caller_role = await add_role(session)
    caller = await add_user(session, role=caller_role)
    target = await add_user(session)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            target.id,
            UserRoleUpdate(role_id=super_admin.id),
            context=context_for(caller, caller_role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    assert error.value.http_status == 403
    await session.refresh(target)
    assert target.role_id is None


async def test_super_admin_can_grant_super_admin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    caller = await add_user(session, role=super_admin, feishu=True)
    target = await add_user(session)

    updated = await assign_user_role(
        session,
        target.id,
        UserRoleUpdate(role_id=super_admin.id),
        context=context_for(caller, super_admin, is_super_admin=True),
        redis=redis,
    )

    assert updated.role_id == super_admin.id


async def test_anyone_cannot_change_own_role(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    user = await add_user(session, role=role)

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            user.id,
            UserRoleUpdate(role_id=None),
            context=context_for(user, role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.CANNOT_MODIFY_SELF
    assert error.value.http_status == 403
    await session.refresh(user)
    assert user.role_id == role.id


async def test_anyone_cannot_change_own_department(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    user = await add_user(session, role=role)
    department = await add_department(session)

    with pytest.raises(BusinessError) as error:
        await assign_user_department(
            session,
            user.id,
            UserDepartmentUpdate(department_id=department.id),
            context=context_for(user, role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.CANNOT_MODIFY_SELF
    assert error.value.http_status == 403
    await session.refresh(user)
    assert user.department_id is None


async def test_anyone_cannot_change_own_status(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    user = await add_user(session, role=role)

    with pytest.raises(BusinessError) as error:
        await update_user_status(
            session,
            user.id,
            UserStatusUpdate(status=UserStatus.DISABLED),
            context=context_for(user, role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.CANNOT_MODIFY_SELF
    assert error.value.http_status == 403
    await session.refresh(user)
    assert user.status == UserStatus.ACTIVE


async def test_cannot_disable_last_super_admin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    admin_role = await add_role(session)
    caller = await add_user(session, role=admin_role)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    target = await add_user(session, role=super_admin, feishu=True)

    with pytest.raises(BusinessError) as error:
        await update_user_status(
            session,
            target.id,
            UserStatusUpdate(status=UserStatus.DISABLED),
            context=context_for(caller, admin_role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.LAST_SUPER_ADMIN
    assert error.value.http_status == 403
    await session.refresh(target)
    assert target.status == UserStatus.ACTIVE


async def test_cannot_demote_last_super_admin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    admin_role = await add_role(session)
    caller = await add_user(session, role=admin_role)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    target = await add_user(session, role=super_admin, feishu=True)

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            target.id,
            UserRoleUpdate(role_id=admin_role.id),
            context=context_for(caller, admin_role, is_super_admin=True),
            redis=redis,
        )

    assert error.value.code == ErrorCode.LAST_SUPER_ADMIN
    assert error.value.http_status == 403
    await session.refresh(target)
    assert target.role_id == super_admin.id


async def test_cannot_delete_local_emergency_superadmin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    caller = await add_user(session, role=super_admin, feishu=True)
    local_emergency = await add_user(session, role=super_admin)

    with pytest.raises(BusinessError) as error:
        await delete_user(
            session,
            local_emergency.id,
            context=context_for(caller, super_admin, is_super_admin=True),
            redis=redis,
        )

    assert error.value.code == ErrorCode.LAST_SUPER_ADMIN
    assert error.value.http_status == 403
    assert await session.get(User, local_emergency.id) is not None


@pytest.mark.parametrize("operation", ["disable", "demote"])
async def test_local_emergency_superadmin_cannot_be_disabled_or_demoted(
    session: AsyncSession,
    redis: RecordingRedis,
    operation: str,
) -> None:
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    caller = await add_user(session, role=super_admin, feishu=True)
    local_emergency = await add_user(session, role=super_admin)
    context = context_for(caller, super_admin, is_super_admin=True)

    with pytest.raises(BusinessError) as error:
        if operation == "disable":
            await update_user_status(
                session,
                local_emergency.id,
                UserStatusUpdate(status=UserStatus.DISABLED),
                context=context,
                redis=redis,
            )
        else:
            await assign_user_role(
                session,
                local_emergency.id,
                UserRoleUpdate(role_id=None),
                context=context,
                redis=redis,
            )

    assert error.value.code == ErrorCode.LAST_SUPER_ADMIN
    assert error.value.http_status == 403


async def test_assigning_user_to_disabled_department_is_rejected(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    department = await add_department(
        session,
        status=DepartmentStatus.DISABLED,
    )

    with pytest.raises(BusinessError) as error:
        await assign_user_department(
            session,
            target.id,
            UserDepartmentUpdate(department_id=department.id),
            context=context_for(caller, role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.http_status == 403
    await session.refresh(target)
    assert target.department_id is None


async def test_forbidden_assignment_returns_403_not_silent_trim(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return context_for(caller, role)

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_redis] = lambda: redis
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.patch(
                f"/api/v1/users/{target.id}/role",
                json={"role_id": super_admin.id},
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    await session.refresh(target)
    assert target.role_id is None


async def test_role_change_bumps_token_version_and_clears_sessions(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    assigned_role = await add_role(session)
    await seed_session_state(redis, target.id)

    updated = await assign_user_role(
        session,
        target.id,
        UserRoleUpdate(role_id=assigned_role.id),
        context=context_for(caller, role),
        redis=redis,
    )

    assert updated.role_id == assigned_role.id
    assert updated.token_version == 1
    assert_session_state_cleared(redis, target.id)


async def test_department_change_bumps_token_version(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    department = await add_department(session)
    await seed_session_state(redis, target.id)

    updated = await assign_user_department(
        session,
        target.id,
        UserDepartmentUpdate(department_id=department.id),
        context=context_for(caller, role),
        redis=redis,
    )

    assert updated.department_id == department.id
    assert updated.token_version == 1
    assert_session_state_cleared(redis, target.id)


async def test_status_change_bumps_token_version_and_clears_sessions(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    await seed_session_state(redis, target.id)

    updated = await update_user_status(
        session,
        target.id,
        UserStatusUpdate(status=UserStatus.DISABLED),
        context=context_for(caller, role),
        redis=redis,
    )

    assert updated.status == UserStatus.DISABLED
    assert updated.token_version == 1
    assert_session_state_cleared(redis, target.id)


def test_assignable_role_sets_respect_enabled_and_super_admin() -> None:
    active_role = Role(
        code="operator",
        name="operator",
        data_scope=DataScope.SELF,
        data_scope_include_children=True,
        is_system=False,
        status=RoleStatus.ACTIVE,
    )
    disabled_role = Role(
        code="disabled",
        name="disabled",
        data_scope=DataScope.SELF,
        data_scope_include_children=True,
        is_system=False,
        status=RoleStatus.DISABLED,
    )
    super_admin = Role(
        code="super_admin",
        name="super_admin",
        data_scope=DataScope.ALL,
        data_scope_include_children=True,
        is_system=True,
        status=RoleStatus.ACTIVE,
    )
    roles = [active_role, disabled_role, super_admin]
    regular_context = AuthContext(
        user_id=1,
        role_id=2,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=ALL_USER_PERMISSIONS,
        is_super_admin=False,
    )
    super_context = AuthContext(
        user_id=1,
        role_id=3,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=ALL_USER_PERMISSIONS,
        is_super_admin=True,
    )

    assert assignable_role_ids(regular_context, roles) == {active_role.id}
    assert assignable_role_ids(super_context, roles) == {
        active_role.id,
        super_admin.id,
    }


async def test_non_super_admin_cannot_update_or_delete_super_admin_role(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    context = context_for(caller, role)

    with pytest.raises(BusinessError) as update_error:
        await update_role(
            session,
            super_admin.id,
            RoleUpdate(description="attempted change"),
            context=context,
            redis=redis,
        )

    assert update_error.value.code == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    assert update_error.value.http_status == 403

    with pytest.raises(BusinessError) as delete_error:
        await delete_role(
            session,
            super_admin.id,
            context=context,
            redis=redis,
        )

    assert delete_error.value.code == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    assert delete_error.value.http_status == 403
    await session.refresh(super_admin)
    assert super_admin.description is None


async def test_non_super_admin_cannot_reset_local_emergency_superadmin_password(
    session: AsyncSession,
) -> None:
    caller_role = await add_role(session)
    caller = await add_user(session, role=caller_role)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    username = unique_code("emergency")
    old_password = "emergency-old-password"
    emergency = await create_local_user(
        session,
        UserCreate(username=username, password=old_password),
    )
    emergency.role_id = super_admin.id
    await session.commit()
    await session.refresh(emergency)

    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return context_for(caller, caller_role)

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/users/{emergency.id}/password/reset",
                json={"new_password": "attacker-password"},
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    authenticated = await auth_service.authenticate_local_user(
        session,
        username,
        old_password,
    )
    assert authenticated.id == emergency.id
    with pytest.raises(BusinessError) as error:
        await auth_service.authenticate_local_user(
            session,
            username,
            "attacker-password",
        )
    assert error.value.code == ErrorCode.INVALID_CREDENTIALS


async def test_invalidation_failure_rolls_back_department_change(
    session: AsyncSession,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    department = await add_department(session)

    with pytest.raises(BusinessError) as error:
        await assign_user_department(
            session,
            target.id,
            UserDepartmentUpdate(department_id=department.id),
            context=context_for(caller, role),
            redis=FailingRedis(),
        )

    assert error.value.code == ErrorCode.INTERNAL_ERROR
    assert error.value.http_status == 503
    assert "roll" in error.value.message.lower()
    await session.refresh(target)
    assert target.department_id is None
    assert target.token_version == 0


async def test_role_change_and_token_bump_are_in_one_transaction_before_invalidation(
    session: AsyncSession,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session)
    assigned_role = await add_role(session)
    assigned_role_id = assigned_role.id
    inspecting_redis = InspectingFailingRedis(session, target.id)

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            target.id,
            UserRoleUpdate(role_id=assigned_role_id),
            context=context_for(caller, role),
            redis=inspecting_redis,
        )

    assert error.value.code == ErrorCode.INTERNAL_ERROR
    assert inspecting_redis.observed == (assigned_role_id, 1)
    await session.refresh(target)
    assert target.role_id is None
    assert target.token_version == 0


async def test_non_super_admin_cannot_revoke_super_admin(
    session: AsyncSession,
    redis: RecordingRedis,
) -> None:
    regular_role = await add_role(session)
    caller = await add_user(session, role=regular_role)
    super_admin = await add_role(
        session,
        code="super_admin",
        is_system=True,
    )
    target = await add_user(session, role=super_admin, feishu=True)

    with pytest.raises(BusinessError) as error:
        await assign_user_role(
            session,
            target.id,
            UserRoleUpdate(role_id=regular_role.id),
            context=context_for(caller, regular_role),
            redis=redis,
        )

    assert error.value.code == ErrorCode.CANNOT_GRANT_SUPER_ADMIN
    assert error.value.http_status == 403
    await session.refresh(target)
    assert target.role_id == super_admin.id


async def test_post_commit_reinvalidation_clears_snapshot_repopulated_during_commit(
    session: AsyncSession,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session, role=role)
    assigned_role = await add_role(session)
    snapshot_key = key("auth", "snapshot", str(target.id))
    stale_snapshot = auth_service.AuthSnapshot(
        status=target.status,
        token_version=target.token_version,
        role_id=target.role_id,
        department_id=target.department_id,
    ).to_cache()
    redis = RaceRepopulatingRedis(snapshot_key, stale_snapshot)

    updated = await assign_user_role(
        session,
        target.id,
        UserRoleUpdate(role_id=assigned_role.id),
        context=context_for(caller, role),
        redis=redis,
    )

    refresh_prefix = key("auth", "refresh", str(target.id), "")
    assert redis.repopulated is True
    assert updated.role_id == assigned_role.id
    assert snapshot_key not in redis.values
    assert not any(name.startswith(refresh_prefix) for name in redis.values)


async def test_post_commit_delete_failure_leaves_fence_that_blocks_stale_snapshot(
    session: AsyncSession,
) -> None:
    role = await add_role(session)
    caller = await add_user(session, role=role)
    target = await add_user(session, role=role)
    assigned_role = await add_role(session)
    snapshot_key = key("auth", "snapshot", str(target.id))
    fence_key = key("auth", "invalidating", str(target.id))
    stale_snapshot = auth_service.AuthSnapshot(
        status=target.status,
        token_version=target.token_version,
        role_id=target.role_id,
        department_id=target.department_id,
    ).to_cache()
    redis = PostCommitDeleteFailingRedis(snapshot_key, stale_snapshot)

    updated = await assign_user_role(
        session,
        target.id,
        UserRoleUpdate(role_id=assigned_role.id),
        context=context_for(caller, role),
        redis=redis,
    )
    snapshot = await auth_service.get_auth_snapshot(session, redis, target.id)

    assert updated.role_id == assigned_role.id
    assert fence_key in redis.values
    assert snapshot_key not in redis.values
    assert snapshot.role_id == assigned_role.id
    assert snapshot.token_version == updated.token_version


async def test_target_visibility_uses_dept_child_and_custom_semantics(
    session: AsyncSession,
) -> None:
    role = await add_role(session)
    parent = await add_department(session)
    child = Department(
        parent_id=parent.id,
        name=unique_code("child"),
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    session.add(child)
    await session.flush()
    child.path = f"{parent.path}{child.id}/"
    await session.commit()
    await session.refresh(child)
    caller = await add_user(session, role=role, department=parent)
    target = await add_user(session, department=child)

    dept_child_context = context_for(
        caller,
        role,
        data_scope=DataScope.DEPT_AND_CHILD,
    )
    await assert_target_visible(session, dept_child_context, target)

    custom_without_children = context_for(
        caller,
        role,
        data_scope=DataScope.CUSTOM,
        custom_department_ids=frozenset({parent.id}),
        include_children=False,
    )
    with pytest.raises(BusinessError) as error:
        await assert_target_visible(session, custom_without_children, target)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.http_status == 403

    custom_with_children = context_for(
        caller,
        role,
        data_scope=DataScope.CUSTOM,
        custom_department_ids=frozenset({parent.id}),
        include_children=True,
    )
    await assert_target_visible(session, custom_with_children, target)
