import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.security import verify_password
from app.db.session import get_session
from app.main import create_app
from app.models.customer import Customer, CustomerSource
from app.models.department import DepartmentStatus
from app.models.role import DataScope, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import customer as customer_repository
from app.repositories import department as department_repository
from app.repositories import user as user_repository
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
    delete_user,
    get_user,
    list_users,
    reset_user_password,
    update_user,
    update_user_status,
)
from tests.conftest import FakeRedis

ALL_SCOPE_CONTEXT = AuthContext(
    user_id=1,
    role_id=1,
    department_id=None,
    data_scope=DataScope.ALL,
    custom_department_ids=frozenset(),
    include_children=True,
    permission_codes=frozenset(
        {"user:create", "user:read", "user:update", "user:delete", "user:assign"}
    ),
    is_super_admin=False,
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
        UserCreate(username=username, password="initial-password1"),
    )

    assert user.username == username
    assert user.password_hash is not None
    assert user.password_hash != "initial-password1"
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password("initial-password1", user.password_hash) is True
    assert user.status == UserStatus.ACTIVE
    assert user.token_version == 0


def test_create_local_user_requires_username_and_password() -> None:
    with pytest.raises(ValidationError):
        UserCreate.model_validate({"password": "initial-password1"})

    with pytest.raises(ValidationError):
        UserCreate.model_validate({"username": "alice"})


async def test_duplicate_username_returns_2001(session: AsyncSession) -> None:
    username = unique_username("duplicate")
    await create_local_user(
        session,
        UserCreate(username=username, password="initial-password1"),
    )

    with pytest.raises(BusinessError) as error:
        await create_local_user(
            session,
            UserCreate(username=username, password="another-password1"),
        )

    assert error.value.code == ErrorCode.USERNAME_EXISTS
    assert error.value.http_status == 409


async def test_duplicate_username_check_uses_normalization(
    session: AsyncSession,
) -> None:
    username = unique_username("normalized-duplicate")
    await create_local_user(
        session,
        UserCreate(
            username=f" {username.upper()} ",
            password="initial-password1",
        ),
    )

    with pytest.raises(BusinessError) as error:
        await create_local_user(
            session,
            UserCreate(
                username=f"  {username.casefold()}  ",
                password="another-password1",
            ),
        )

    assert error.value.code == ErrorCode.USERNAME_EXISTS


@pytest.mark.parametrize("invalid_username", ["   ", " a ", "a" * 65])
async def test_user_creation_rejects_invalid_normalized_username(
    session: AsyncSession,
    invalid_username: str,
) -> None:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return ALL_SCOPE_CONTEXT

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
                    "username": invalid_username,
                    "password": "initial-password1",
                },
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.INVALID_PARAMETER
    assert "Username" in response.json()["message"]
    normalized = invalid_username.strip().casefold()
    assert await session.scalar(select(User).where(User.username == normalized)) is None


async def test_create_user_without_role_or_department_is_allowed(
    session: AsyncSession,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("unassigned"), password="password1"),
    )

    assert user.role_id is None
    assert user.department_id is None


async def test_get_user_raises_user_not_found(session: AsyncSession) -> None:
    with pytest.raises(BusinessError) as error:
        await get_user(session, 999_999_999, context=ALL_SCOPE_CONTEXT)

    assert error.value.code == ErrorCode.USER_NOT_FOUND
    assert error.value.http_status == 404


async def test_patch_user_updates_profile_fields(session: AsyncSession) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("profile"), password="password1"),
    )

    updated = await update_user(
        session,
        user.id,
        UserUpdate(
            nickname="Alice",
            email="alice@example.com",
            avatar="https://example.com/alice.png",
        ),
        context=ALL_SCOPE_CONTEXT,
    )

    assert updated.nickname == "Alice"
    assert updated.email == "alice@example.com"
    assert updated.avatar == "https://example.com/alice.png"


async def test_assignments_and_status_have_separate_operations(
    session: AsyncSession,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("assignment"), password="password1"),
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
        context=ALL_SCOPE_CONTEXT,
    )
    assigned_role = await assign_user_role(
        session,
        user.id,
        UserRoleUpdate(role_id=role.id),
        context=ALL_SCOPE_CONTEXT,
    )
    disabled = await update_user_status(
        session,
        user.id,
        UserStatusUpdate(status=UserStatus.DISABLED),
        context=ALL_SCOPE_CONTEXT,
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
            UserPasswordReset(new_password="new-password1"),
            context=ALL_SCOPE_CONTEXT,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert "local" in error.value.message.lower()


async def test_password_reset_updates_local_password(session: AsyncSession) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("local-reset"), password="old-password1"),
    )

    reset = await reset_user_password(
        session,
        user.id,
        UserPasswordReset(new_password="new-password1"),
        context=ALL_SCOPE_CONTEXT,
    )

    assert reset.password_hash is not None
    assert verify_password("new-password1", reset.password_hash) is True
    assert verify_password("old-password1", reset.password_hash) is False


async def test_list_users_filters_and_paginates(session: AsyncSession) -> None:
    username = unique_username("listed")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password1"),
    )
    await update_user(
        session,
        user.id,
        UserUpdate(nickname="Listed User", email="listed@example.com"),
        context=ALL_SCOPE_CONTEXT,
    )

    items, total = await list_users(
        session,
        context=ALL_SCOPE_CONTEXT,
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
        UserCreate(username=unique_username("dept-user"), password="password1"),
    )
    await assign_user_department(
        session,
        user.id,
        UserDepartmentUpdate(department_id=department.id),
        context=ALL_SCOPE_CONTEXT,
    )

    with pytest.raises(BusinessError) as error:
        await delete_department(session, department.id)

    assert error.value.code == ErrorCode.DEPT_HAS_USERS
    assert await get_user(session, user.id, context=ALL_SCOPE_CONTEXT) is not None


async def test_delete_role_with_real_user_returns_3002(
    session: AsyncSession,
) -> None:
    role = await create_role(
        session,
        RoleCreate(code=unique_username("in-use-role"), name="使用中的角色"),
    )
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("role-user"), password="password1"),
    )
    await assign_user_role(
        session,
        user.id,
        UserRoleUpdate(role_id=role.id),
        context=ALL_SCOPE_CONTEXT,
    )

    with pytest.raises(BusinessError) as error:
        await delete_role(
            session,
            role.id,
            context=ALL_SCOPE_CONTEXT,
        )

    assert error.value.code == ErrorCode.ROLE_IN_USE
    assert await get_user(session, user.id, context=ALL_SCOPE_CONTEXT) is not None


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

    async def override_context() -> AuthContext:
        return ALL_SCOPE_CONTEXT

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    username = unique_username("api")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/users",
                json={"username": username, "password": "initial-password1"},
            )
            user_id = created.json()["data"]["id"]
            loaded = await client.get(f"/api/v1/users/{user_id}")
            updated = await client.patch(
                f"/api/v1/users/{user_id}",
                json={"nickname": "API User"},
            )
            reset = await client.post(
                f"/api/v1/users/{user_id}/password/reset",
                json={"new_password": "new-password1"},
            )
            forbidden_write = await client.post(
                "/api/v1/users",
                json={
                    "username": unique_username("feishu-write"),
                    "password": "password1",
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
        UserCreate(username=unique_username("profile-only"), password="password1"),
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

    loaded = await get_user(session, user.id, context=ALL_SCOPE_CONTEXT)
    assert loaded.role_id is None
    assert loaded.department_id is None


async def test_delete_user_with_owned_customers_returns_2005(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("owner"), password="password1"),
    )
    session.add(
        Customer(
            name="归属客户",
            source=CustomerSource.MANUAL,
            owner_user_id=user.id,
            owner_department_id=user.department_id,
        )
    )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await delete_user(
            session,
            user.id,
            context=ALL_SCOPE_CONTEXT,
            redis=fake_redis,
        )

    assert error.value.code == ErrorCode.USER_HAS_CUSTOMERS
    assert error.value.http_status == 409
    assert await user_repository.get_by_id(session, user.id) is not None


async def test_delete_user_race_with_new_customer_returns_2005(
    session: AsyncSession,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("race-owner"), password="password1"),
    )
    session.add(
        Customer(
            name="并发归属客户",
            source=CustomerSource.MANUAL,
            owner_user_id=user.id,
            owner_department_id=user.department_id,
        )
    )
    await session.flush()

    # 模拟并发 TOCTOU：预检查时返回 False（假装当时还没有客户），但表里其实
    # 已有归属该用户的客户行，于是删除会触发 owner_user_id 外键 RESTRICT；
    # 随后的复核委托真实实现，返回 True 以命中业务错误。
    real_has_customers = customer_repository.has_customers
    call_count = {"n": 0}

    async def _precheck_misses_customer(_session: AsyncSession, _user_id: int) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return False
        return await real_has_customers(_session, _user_id)

    monkeypatch.setattr(
        "app.services.user.customer_repository.has_customers",
        _precheck_misses_customer,
    )

    # 复核必须走加锁的当前读，否则 REPEATABLE READ 快照可能漏判并发提交的客户。
    locking_calls = {"n": 0}
    real_has_customers_for_update = customer_repository.has_customers_for_update

    async def _spy_has_customers_for_update(
        _session: AsyncSession, _user_id: int
    ) -> bool:
        locking_calls["n"] += 1
        return await real_has_customers_for_update(_session, _user_id)

    monkeypatch.setattr(
        "app.services.user.customer_repository.has_customers_for_update",
        _spy_has_customers_for_update,
    )

    with pytest.raises(BusinessError) as error:
        await delete_user(
            session,
            user.id,
            context=ALL_SCOPE_CONTEXT,
            redis=fake_redis,
        )

    assert locking_calls["n"] == 1
    assert error.value.code == ErrorCode.USER_HAS_CUSTOMERS
    assert error.value.http_status == 409
    assert await user_repository.get_by_id(session, user.id) is not None


async def test_has_customers_for_update_sees_row_committed_after_snapshot() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    reader: AsyncSession | None = None
    try:
        async with factory() as setup:
            owner = User(
                feishu_open_id=None,
                username=unique_username("current-read-owner"),
                password_hash="x",
                nickname=None,
                email=None,
                avatar=None,
                department_id=None,
                role_id=None,
                status=UserStatus.ACTIVE,
                token_version=0,
                last_login_at=None,
            )
            setup.add(owner)
            await setup.commit()
            user_id = owner.id
        assert user_id is not None

        # 连接 A 先执行一次普通读固定 REPEATABLE READ 快照，此时该用户还没有客户。
        reader = factory()
        assert await customer_repository.has_customers(reader, user_id) is False

        # 连接 B 并发插入并提交一条归属该用户的客户。
        async with factory() as writer:
            writer.add(
                Customer(
                    name="快照后新增客户",
                    source=CustomerSource.MANUAL,
                    owner_user_id=user_id,
                    owner_department_id=None,
                )
            )
            await writer.commit()

        # A 的普通读仍停留在旧快照（看不到新行）；加锁读是当前读，能看到新行。
        assert await customer_repository.has_customers(reader, user_id) is False
        assert (
            await customer_repository.has_customers_for_update(reader, user_id) is True
        )
    finally:
        if reader is not None:
            await reader.rollback()
            await reader.close()
        async with factory() as cleanup:
            if user_id is not None:
                await cleanup.execute(
                    delete(Customer).where(Customer.owner_user_id == user_id)
                )
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
        await engine.dispose()


async def test_delete_user_without_customers_succeeds(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    user = await create_local_user(
        session,
        UserCreate(username=unique_username("no-customers"), password="password1"),
    )

    await delete_user(
        session,
        user.id,
        context=ALL_SCOPE_CONTEXT,
        redis=fake_redis,
    )

    assert await user_repository.get_by_id(session, user.id) is None
