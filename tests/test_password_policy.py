import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.role import DataScope
from app.models.user import User

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

USER_CONTEXT = AuthContext(
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
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return USER_CONTEXT

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        application.dependency_overrides.clear()


async def test_create_and_reset_enforce_password_policy(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = f"policy-{uuid4().hex[:12]}"
    password = "abc12345"
    created = await client.post(
        "/api/v1/users",
        json={"username": username, "password": password},
    )

    assert created.status_code == 201
    user_id = created.json()["data"]["id"]

    invalid_passwords = ("abc1234", "abcdefgh", "12345678", "        ")
    for invalid_password in invalid_passwords:
        create_response = await client.post(
            "/api/v1/users",
            json={
                "username": f"{username}-{len(invalid_password)}",
                "password": invalid_password,
            },
        )
        reset_response = await client.post(
            f"/api/v1/users/{user_id}/password/reset",
            json={"new_password": invalid_password},
        )

        assert create_response.status_code == 422
        assert create_response.json()["code"] == ErrorCode.INVALID_PARAMETER
        assert "8" in create_response.json()["message"]
        assert reset_response.status_code == 422
        assert reset_response.json()["code"] == ErrorCode.INVALID_PARAMETER
        assert "8" in reset_response.json()["message"]

    invalid_usernames = {
        f"{username}-{len(invalid_password)}" for invalid_password in invalid_passwords
    }
    invalid_users = list(
        (
            await session.scalars(
                select(User).where(User.username.in_(invalid_usernames))
            )
        ).all()
    )
    assert invalid_users == []

    valid_reset = await client.post(
        f"/api/v1/users/{user_id}/password/reset",
        json={"new_password": "xyz98765"},
    )
    assert valid_reset.status_code == 200
