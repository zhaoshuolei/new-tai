import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.models.role import DataScope, Role, RoleStatus
from app.models.user import User, UserStatus
from app.services.auth import build_auth_context

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@pytest_asyncio.fixture
async def session_with_user() -> AsyncIterator[tuple[AsyncSession, User]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            role = Role(
                code="operator",
                name="Operator",
                description=None,
                data_scope=DataScope.SELF,
                data_scope_include_children=False,
                is_system=False,
                status=RoleStatus.ACTIVE,
            )
            session.add(role)
            await session.flush()
            user = User(
                feishu_open_id=None,
                username="operator",
                password_hash=None,
                nickname="操作员",
                email=None,
                avatar=None,
                department_id=None,
                role_id=role.id,
                status=UserStatus.ACTIVE,
                token_version=0,
                last_login_at=None,
            )
            session.add(user)
            await session.flush()
            yield session, user
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def test_auth_context_defaults() -> None:
    context = AuthContext(
        user_id=1,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(),
        is_super_admin=False,
    )

    assert context.username is None
    assert context.nickname is None


async def test_build_auth_context_carries_actor(
    session_with_user: tuple[AsyncSession, User],
) -> None:
    session, user = session_with_user

    context = await build_auth_context(session, user)

    assert context.username == "operator"
    assert context.nickname == "操作员"


async def test_build_auth_context_carries_actor_without_role(
    session_with_user: tuple[AsyncSession, User],
) -> None:
    session, user = session_with_user
    user.role_id = None

    context = await build_auth_context(session, user)

    assert context.username == "operator"
    assert context.nickname == "操作员"
