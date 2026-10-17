"""Current-user profile API tests."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
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
    context: AuthContext | None = None,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    if context is not None:

        async def override_context(request: Request) -> AuthContext:
            request.state.user_id = context.user_id
            request.state.actor = ActorSnapshot(
                user_id=context.user_id,
                username=context.username,
                nickname=context.nickname,
            )
            return context

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
        nickname="Profile Owner",
    )


async def _make_user(
    session: AsyncSession,
    *,
    department_id: int | None = None,
) -> User:
    user = User(
        feishu_open_id=f"ou_{uuid4().hex}",
        username=f"profile-api-{uuid4().hex[:12]}",
        nickname="Profile Owner",
        email="owner@example.com",
        avatar="/media/avatars/owner.png",
        department_id=department_id,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_deleted_department(session: AsyncSession) -> Department:
    department = Department(
        name="档案部",
        path=f"/profile-{uuid4().hex}",
        status=DepartmentStatus.ACTIVE,
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(department)
    await session.flush()
    return department


async def test_get_my_profile_returns_complete_edit_view(
    session: AsyncSession,
) -> None:
    department = await _make_deleted_department(session)
    user = await _make_user(session, department_id=department.id)
    profile = UserProfile(
        user_id=user.id,
        signature="真实签名",
        bio="真实简介",
        work_phone="010-12345678",
        extension="801",
        work_email="employee@example.com",
        office_location="北京",
        seat="A-01",
        expertise_tags=["Python", "SQLAlchemy"],
        personal_links=[
            {"label": "个人主页", "url": "https://example.com/profile"},
        ],
        hidden_public_fields=["signature", "bio"],
        personal_phone="13800000000",
        birthday=date(1990, 5, 20),
        emergency_contact_name="张三",
        emergency_contact_phone="13900000000",
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert payload["code"] == 0
    assert data["id"] == user.id
    assert data["username"] == user.username
    assert data["account_email"] == "owner@example.com"
    assert data["department_id"] == department.id
    assert data["department_name"] == "档案部"
    assert data["user_status"] == "active"
    assert data["feishu_open_id"] == user.feishu_open_id
    assert data["nickname"] == "Profile Owner"
    assert data["avatar"] == "/media/avatars/owner.png"
    assert data["signature"] == "真实签名"
    assert data["bio"] == "真实简介"
    assert data["work_phone"] == "010-12345678"
    assert data["extension"] == "801"
    assert data["work_email"] == "employee@example.com"
    assert data["office_location"] == "北京"
    assert data["seat"] == "A-01"
    assert data["expertise_tags"] == ["Python", "SQLAlchemy"]
    assert data["personal_links"] == [
        {"label": "个人主页", "url": "https://example.com/profile"},
    ]
    assert data["hidden_public_fields"] == ["signature", "bio"]
    assert data["personal_phone"] == "13800000000"
    assert data["birthday"] == "1990-05-20"
    assert isinstance(data["birthday"], str)
    assert data["emergency_contact_name"] == "张三"
    assert data["emergency_contact_phone"] == "13900000000"
    assert "created_at" not in data
    assert "email" not in data
    assert "status" not in data


async def test_get_my_profile_defaults_missing_profile_and_json_fields(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)

    async with api_client(session, _context(user.id)) as client:
        response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["expertise_tags"] == []
    assert data["personal_links"] == []
    assert data["hidden_public_fields"] == []
    assert data["signature"] is None
    assert data["birthday"] is None


async def test_get_my_profile_ignores_public_field_hiding(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    session.add(
        UserProfile(
            user_id=user.id,
            signature="仅本人可见的签名",
            bio="仅本人可见的简介",
            hidden_public_fields=["signature", "bio"],
        )
    )
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["signature"] == "仅本人可见的签名"
    assert data["bio"] == "仅本人可见的简介"


async def test_get_my_profile_returns_not_found_for_missing_user(
    session: AsyncSession,
) -> None:
    async with api_client(session, _context(2_000_000_000)) as client:
        response = await client.get("/api/v1/me/profile")

    assert response.status_code == 404
    assert response.json()["code"] == 2000
