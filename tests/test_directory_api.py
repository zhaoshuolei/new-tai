"""Colleague directory API tests."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile

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


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context(request: Request) -> AuthContext:
        request.state.user_id = context.user_id
        request.state.actor = ActorSnapshot(
            user_id=context.user_id,
            username=context.username,
            nickname=context.nickname,
        )
        return context

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


def _context(user_id: int) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(),
        is_super_admin=False,
        username=f"context-{user_id}",
        nickname="Directory Reader",
    )


async def _make_department(
    session: AsyncSession,
    *,
    name: str,
    sort_order: int = 0,
    status: DepartmentStatus = DepartmentStatus.ACTIVE,
    is_system: bool = False,
    deleted_at: datetime | None = None,
) -> Department:
    department = Department(
        name=name,
        path=f"/directory-api-{uuid4().hex}",
        sort_order=sort_order,
        status=status,
        is_system=is_system,
        deleted_at=deleted_at,
    )
    session.add(department)
    await session.flush()
    return department


async def _make_user(
    session: AsyncSession,
    *,
    username: str,
    nickname: str | None,
    department_id: int | None = None,
    avatar: str | None = None,
) -> User:
    user = User(
        feishu_open_id=f"ou_{uuid4().hex}",
        username=username,
        nickname=nickname,
        avatar=avatar,
        department_id=department_id,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def test_directory_list_requires_no_user_read_permission(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="通讯录权限部")
    token = uuid4().hex[:12]
    user = await _make_user(
        session,
        username=f"directory-{token}",
        nickname="Directory User",
        department_id=department.id,
        avatar="/media/avatars/directory.png",
    )

    async with api_client(session, _context(user.id)) as client:
        response = await client.get(
            "/api/v1/directory",
            params={"keyword": token, "page": 1, "page_size": 10},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["total"] == 1
    item = payload["data"]["items"][0]
    assert set(item) == {
        "id",
        "display_name",
        "nickname",
        "avatar",
        "signature",
        "department_name",
    }
    assert item["id"] == user.id
    assert item["department_name"] == "通讯录权限部"


async def test_directory_list_clips_hidden_card_fields(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="通讯录隐私部")
    token = uuid4().hex[:12]
    user = await _make_user(
        session,
        username=f"hidden-{token}",
        nickname="Hidden User",
        department_id=department.id,
        avatar="/media/avatars/hidden.png",
    )
    session.add(
        UserProfile(
            user_id=user.id,
            signature="do not expose",
            hidden_public_fields=["avatar", "signature"],
        )
    )
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.get("/api/v1/directory", params={"keyword": token})

    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["avatar"] is None
    assert item["signature"] is None


async def test_department_filter_options_exclude_system_disabled_and_deleted(
    session: AsyncSession,
) -> None:
    visible = await _make_department(
        session,
        name="可见部门",
        sort_order=-100,
    )
    system = await _make_department(
        session,
        name="待分配",
        sort_order=-110,
        is_system=True,
    )
    disabled = await _make_department(
        session,
        name="停用部门",
        sort_order=-120,
        status=DepartmentStatus.DISABLED,
    )
    deleted = await _make_department(
        session,
        name="已删除部门",
        sort_order=-130,
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    visible_tie = await _make_department(
        session,
        name="同序部门",
        sort_order=-100,
    )

    async with api_client(session, _context(visible.id)) as client:
        response = await client.get("/api/v1/directory/departments")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    departments = payload["data"]
    by_id = {department["id"]: department for department in departments}
    assert visible.id in by_id
    assert visible_tie.id in by_id
    assert system.id not in by_id
    assert disabled.id not in by_id
    assert deleted.id not in by_id
    assert departments == sorted(
        departments,
        key=lambda department: (department["sort_order"], department["id"]),
    )
    assert set(by_id[visible.id]) == {"id", "name", "parent_id", "sort_order"}
