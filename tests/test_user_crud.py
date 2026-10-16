import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.core.security import verify_password
from app.db.session import get_session
from app.main import create_app
from app.models.department import DepartmentStatus
from app.models.role import DataScope, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import department as department_repository
from app.schemas.department import DepartmentCreate
from app.schemas.role import RoleCreate, RoleDataScopeUpdate
from app.schemas.user import (
    UserCreate,
    UserDepartmentUpdate,
    UserPasswordReset,
    UserRoleUpdate,
    UserStatusUpdate,
    UserUpdate,
)
from app.services.department import create_department, delete_department
from app.services.role import create_role, delete_role, set_role_data_scope
from app.services.user import (
    assign_user_department,
    assign_user_role,
    create_local_user,
    get_user,
    list_users,
    reset_user_password,
    update_user,
    update_user_status,
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


def unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def test_create_local_user_hashes_password(session: AsyncSession) -> None:
    username = unique_username("alice")

    user = await create_local_user(
        session,
        UserCreate(username=username, password="initial-password"),
    )

    assert user.username == username
    assert user.password_hash is not None
    assert user.password_hash != "initial-password"
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password("initial-password", user.password_hash) is True
    assert user.status == UserStatus.ACTIVE
    assert user.token_version == 0


def test_create_local_user_requires_username_and_password() -> None:
    with pytest.raises(ValidationError):
        UserCreate.model_validate({"password": "initial-password"})

    with pytest.raises(ValidationError):
        UserCreate.model_validate({"username": "alice"})


async def test_duplicate_username_returns_2001(session: AsyncSession) -> None:
    username = unique_username("duplicate")
    await create_local_user(
        session,
        UserCreate(username=username, password="initial-password"),
    )

    with pytest.raises(BusinessError) as error:
        await create_local_user(
            session,
            UserCreate(username=username, password="another-password"),
        )

    assert error.value.code == ErrorCode.USERNAME_EXISTS
    assert error.value.http_status == 409


async def test_create_user_without_role_or_department_is_allowed(
    session: AsyncSession,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("unassigned"), password="password"),
    )

    assert user.role_id is None
    assert user.department_id is None


async def test_get_user_raises_user_not_found(session: AsyncSession) -> None:
    with pytest.raises(BusinessError) as error:
        await get_user(session, 999_999_999)

    assert error.value.code == ErrorCode.USER_NOT_FOUND
    assert error.value.http_status == 404


async def test_patch_user_updates_profile_fields(session: AsyncSession) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("profile"), password="password"),
    )

    updated = await update_user(
        session,
        user.id,
        UserUpdate(
            nickname="Alice",
            email="alice@example.com",
            avatar="https://example.com/alice.png",
        ),
    )

    assert updated.nickname == "Alice"
    assert updated.email == "alice@example.com"
    assert updated.avatar == "https://example.com/alice.png"


async def test_assignments_and_status_have_separate_operations(
    session: AsyncSession,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("assignment"), password="password"),
    )
    department = await create_department(session, DepartmentCreate(name="研发部"))
    role = await create_role(
        session,
        RoleCreate(code=unique_username("role"), name="研发角色"),
    )

    assigned_department = await assign_user_department(
        session,
        user.id,
        UserDepartmentUpdate(department_id=department.id),
    )
    assigned_role = await assign_user_role(
        session,
        user.id,
        UserRoleUpdate(role_id=role.id),
    )
    disabled = await update_user_status(
        session,
        user.id,
        UserStatusUpdate(status=UserStatus.DISABLED),
    )

    assert assigned_department.department_id == department.id
    assert assigned_role.role_id == role.id
    assert disabled.status == UserStatus.DISABLED


async def test_password_reset_on_feishu_user_returns_error(
    session: AsyncSession,
) -> None:
    feishu_user = User(
        feishu_open_id=f"ou-{uuid4().hex}",
        username=None,
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(feishu_user)
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await reset_user_password(
            session,
            feishu_user.id,
            UserPasswordReset(new_password="new-password"),
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert "local" in error.value.message.lower()


async def test_password_reset_updates_local_password(session: AsyncSession) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("local-reset"), password="old-password"),
    )

    reset = await reset_user_password(
        session,
        user.id,
        UserPasswordReset(new_password="new-password"),
    )

    assert reset.password_hash is not None
    assert verify_password("new-password", reset.password_hash) is True
    assert verify_password("old-password", reset.password_hash) is False


async def test_list_users_filters_and_paginates(session: AsyncSession) -> None:
    username = unique_username("listed")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    await update_user(
        session,
        user.id,
        UserUpdate(nickname="Listed User", email="listed@example.com"),
    )

    items, total = await list_users(
        session,
        page=1,
        page_size=10,
        keyword=username,
    )

    assert total == 1
    assert [item.id for item in items] == [user.id]


async def test_delete_department_with_real_user_returns_5002(
    session: AsyncSession,
) -> None:
    department = await create_department(session, DepartmentCreate(name="有用户的部门"))
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("dept-user"), password="password"),
    )
    await assign_user_department(
        session,
        user.id,
        UserDepartmentUpdate(department_id=department.id),
    )

    with pytest.raises(BusinessError) as error:
        await delete_department(session, department.id)

    assert error.value.code == ErrorCode.DEPT_HAS_USERS
    assert await get_user(session, user.id) is not None


async def test_delete_role_with_real_user_returns_3002(
    session: AsyncSession,
) -> None:
    role = await create_role(
        session,
        RoleCreate(code=unique_username("in-use-role"), name="使用中的角色"),
    )
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("role-user"), password="password"),
    )
    await assign_user_role(
        session,
        user.id,
        UserRoleUpdate(role_id=role.id),
    )

    with pytest.raises(BusinessError) as error:
        await delete_role(session, role.id)

    assert error.value.code == ErrorCode.ROLE_IN_USE
    assert await get_user(session, user.id) is not None


async def test_delete_department_referenced_by_custom_role_scope_returns_5005(
    session: AsyncSession,
) -> None:
    department = await create_department(
        session,
        DepartmentCreate(name="角色自定义范围部门"),
    )
    role = await create_role(
        session,
        RoleCreate(code=unique_username("custom-scope-role"), name="自定义范围角色"),
    )
    await set_role_data_scope(
        session,
        role.id,
        RoleDataScopeUpdate(
            data_scope=DataScope.CUSTOM,
            department_ids=[department.id],
        ),
    )

    with pytest.raises(BusinessError) as error:
        await delete_department(session, department.id)

    assert error.value.code == ErrorCode.DEPT_REFERENCED_BY_ROLE
    assert error.value.http_status == 409
    assert "角色" in error.value.message
    assert await department_repository.get_by_id(session, department.id) is not None


async def test_user_api_crud_and_extra_fields_are_rejected(
    session: AsyncSession,
) -> None:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    username = unique_username("api")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/users",
                json={"username": username, "password": "initial-password"},
            )
            user_id = created.json()["data"]["id"]
            loaded = await client.get(f"/api/v1/users/{user_id}")
            updated = await client.patch(
                f"/api/v1/users/{user_id}",
                json={"nickname": "API User"},
            )
            reset = await client.post(
                f"/api/v1/users/{user_id}/password/reset",
                json={"new_password": "new-password"},
            )
            forbidden_write = await client.post(
                "/api/v1/users",
                json={
                    "username": unique_username("feishu-write"),
                    "password": "password",
                    "feishu_open_id": "ou-should-not-be-writable",
                },
            )
            patch_extra = await client.patch(
                f"/api/v1/users/{user_id}",
                json={"role_id": 1},
            )
    finally:
        application.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["data"]["username"] == username
    assert "password_hash" not in created.json()["data"]
    assert loaded.status_code == 200
    assert updated.json()["data"]["nickname"] == "API User"
    assert reset.status_code == 200
    assert forbidden_write.status_code == 422
    assert patch_extra.status_code == 422


async def test_role_and_department_are_not_writable_through_profile_patch(
    session: AsyncSession,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("profile-only"), password="password"),
    )
    role = await create_role(
        session,
        RoleCreate(code=unique_username("unused-role"), name="未分配角色"),
    )
    department = await create_department(session, DepartmentCreate(name="未分配部门"))
    assert role.status == RoleStatus.ACTIVE
    assert department.status == DepartmentStatus.ACTIVE

    with pytest.raises(ValidationError):
        UserUpdate.model_validate({"role_id": role.id})

    with pytest.raises(ValidationError):
        UserUpdate.model_validate({"department_id": department.id})

    loaded = await get_user(session, user.id)
    assert loaded.role_id is None
    assert loaded.department_id is None
