"""Current-user profile API tests."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from asyncmy.errors import IntegrityError as AsyncMyIntegrityError
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.department import Department, DepartmentStatus
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile
from app.repositories import user_profile as user_profile_repository
from app.services import audit as audit_service
from app.services import user_profile as user_profile_service

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


@pytest_asyncio.fixture(autouse=True)
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
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


async def _profile_for(
    session: AsyncSession,
    user_id: int,
) -> UserProfile | None:
    return await session.scalar(
        select(UserProfile).where(UserProfile.user_id == user_id)
    )


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as audit_session:
        rows = await audit_session.scalars(
            select(AuditLog).order_by(AuditLog.id)
        )
        return list(rows.all())


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


async def test_patch_my_profile_updates_scalar_and_returns_fresh_profile(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        signature="old signature",
        seat=None,
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"signature": "hi"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "updated"
    assert payload["data"]["signature"] == "hi"
    assert payload["data"]["seat"] is None

    await session.refresh(profile)
    assert profile.signature == "hi"
    assert profile.seat is None


async def test_patch_nickname_changes_user_without_bumping_token_version(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    original_token_version = user.token_version
    session.add(UserProfile(user_id=user.id, seat=None))
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"nickname": "New Name", "seat": "A1"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["nickname"] == "New Name"
    assert response.json()["data"]["seat"] == "A1"

    await session.refresh(user)
    profile = await _profile_for(session, user.id)
    assert profile is not None
    assert user.nickname == "New Name"
    assert user.token_version == original_token_version
    assert profile.seat == "A1"


async def test_patch_explicit_null_clears_and_absent_field_is_unchanged(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        signature="clear me",
        seat="A1",
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        cleared = await client.patch(
            "/api/v1/me/profile",
            json={"signature": None},
        )
        absent = await client.patch(
            "/api/v1/me/profile",
            json={"seat": "B2"},
        )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["signature"] is None
    assert cleared.json()["data"]["seat"] == "A1"
    assert absent.status_code == 200, absent.text
    assert absent.json()["data"]["signature"] is None
    assert absent.json()["data"]["seat"] == "B2"

    await session.refresh(profile)
    assert profile.signature is None
    assert profile.seat == "B2"


async def test_patch_replaces_json_lists_and_persists_empty_list(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        expertise_tags=["old-tag"],
        personal_links=[
            {"label": "Old", "url": "https://old.example/profile"},
        ],
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={
                "expertise_tags": [],
                "personal_links": [
                    {"label": "Docs", "url": "https://docs.example/profile"},
                ],
            },
        )
        read_response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200, response.text
    expected_links = [
        {"label": "Docs", "url": "https://docs.example/profile"},
    ]
    assert response.json()["data"]["expertise_tags"] == []
    assert response.json()["data"]["personal_links"] == expected_links
    assert read_response.status_code == 200, read_response.text
    assert read_response.json()["data"]["expertise_tags"] == []
    assert read_response.json()["data"]["personal_links"] == expected_links

    await session.refresh(profile)
    assert profile.expertise_tags == []
    assert profile.personal_links == expected_links


async def test_patch_explicit_null_lists_persist_as_empty_lists(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        expertise_tags=["old-tag"],
        personal_links=[
            {"label": "Old", "url": "https://old.example/profile"},
        ],
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"expertise_tags": None, "personal_links": None},
        )
        read_response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["expertise_tags"] == []
    assert response.json()["data"]["personal_links"] == []
    assert read_response.status_code == 200, read_response.text
    assert read_response.json()["data"]["expertise_tags"] == []
    assert read_response.json()["data"]["personal_links"] == []

    await session.refresh(profile)
    assert profile.expertise_tags == []
    assert profile.personal_links == []


async def test_first_patch_creates_missing_profile_row(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"bio": "first profile value"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["bio"] == "first profile value"
    profile = await _profile_for(session, user.id)
    assert profile is not None
    assert profile.bio == "first profile value"
    assert profile.expertise_tags == []
    assert profile.personal_links == []
    assert profile.hidden_public_fields == []


async def test_empty_patch_returns_profile_without_writes_or_audit(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _make_user(session)

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch("/api/v1/me/profile", json={})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == user.id
    assert response.json()["data"]["expertise_tags"] == []
    assert await _profile_for(session, user.id) is None
    assert await _audit_rows(audit_session_factory) == []
    assert user.nickname == "Profile Owner"


async def test_normal_patch_creates_one_audit_without_field_values(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _make_user(session)
    session.add(UserProfile(user_id=user.id))
    await session.flush()
    nickname_marker = "audit-nickname-marker"
    bio_marker = "audit-bio-value-must-not-be-recorded"

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"nickname": nickname_marker, "bio": bio_marker},
        )

    assert response.status_code == 200, response.text
    rows = await _audit_rows(audit_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "user_profile:update"
    assert row.resource_type == "user_profile"
    assert row.resource_id == user.id
    assert row.result == AuditResult.SUCCESS

    serialized = repr(
        {
            column.name: getattr(row, column.name)
            for column in AuditLog.__table__.columns
        }
    )
    assert nickname_marker not in serialized
    assert bio_marker not in serialized


async def test_invalid_patch_writes_nothing_and_creates_no_success_audit(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _make_user(session)
    profile = UserProfile(user_id=user.id, bio="original bio")
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={
                "nickname": "must-not-persist",
                "bio": "x" * 2001,
            },
        )

    assert response.status_code == 422
    await session.refresh(user)
    await session.refresh(profile)
    assert user.nickname == "Profile Owner"
    assert profile.bio == "original bio"
    assert await _audit_rows(audit_session_factory) == []


async def test_patch_cannot_write_avatar(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _make_user(session)
    original_avatar = user.avatar

    async with api_client(session, _context(user.id)) as client:
        response = await client.patch(
            "/api/v1/me/profile",
            json={"avatar": "https://evil.example/x.png"},
        )

    assert response.status_code == 422
    await session.refresh(user)
    assert user.avatar == original_avatar
    assert await _audit_rows(audit_session_factory) == []


async def test_concurrent_first_patch_creates_exactly_one_profile_row(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    try:
        async with factory() as setup_session:
            user = await _make_user(setup_session)
            await setup_session.commit()
            user_id = user.id

        async def patch_once() -> object:
            async with factory() as request_session:
                async with api_client(
                    request_session,
                    _context(user_id),
                ) as client:
                    return await client.patch(
                        "/api/v1/me/profile",
                        json={"bio": "concurrent first write"},
                    )

        responses = await asyncio.gather(patch_once(), patch_once())
        assert [response.status_code for response in responses] == [200, 200], [
            response.text for response in responses
        ]

        async with factory() as verification_session:
            profiles = list(
                (
                    await verification_session.scalars(
                        select(UserProfile).where(UserProfile.user_id == user_id)
                    )
                ).all()
            )
        assert len(profiles) == 1
        assert profiles[0].bio == "concurrent first write"
        audit_rows = await _audit_rows(audit_session_factory)
        assert len(audit_rows) == 2
        assert [row.resource_id for row in audit_rows] == [user_id, user_id]
        assert all(row.action == "user_profile:update" for row in audit_rows)
    finally:
        if user_id is not None:
            async with factory() as cleanup_session:
                await cleanup_session.execute(
                    delete(UserProfile).where(UserProfile.user_id == user_id)
                )
                await cleanup_session.execute(delete(User).where(User.id == user_id))
                await cleanup_session.commit()
        await engine.dispose()


async def test_first_patch_recovers_from_injected_duplicate_key_race(
    audit_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    winner_created = False
    insert_attempts = 0
    recovery_lock_calls = 0
    duplicate_error = IntegrityError(
        "INSERT INTO user_profile (user_id) VALUES (%s)",
        {},
        AsyncMyIntegrityError(
            1062,
            "Duplicate entry '1' for key 'user_profile.user_id'",
        ),
    )
    assert user_profile_service._is_duplicate_profile(duplicate_error)
    original_lock_by_user_id = user_profile_repository.lock_by_user_id

    async def duplicate_add(
        _session: AsyncSession,
        candidate: UserProfile,
    ) -> UserProfile:
        nonlocal insert_attempts, winner_created
        insert_attempts += 1
        async with factory() as winner_session:
            winner_session.add(
                UserProfile(
                    user_id=candidate.user_id,
                    bio="out-of-band race winner",
                )
            )
            await winner_session.commit()
        winner_created = True
        raise duplicate_error

    async def tracking_lock_by_user_id(
        lock_session: AsyncSession,
        lock_user_id: int,
    ) -> UserProfile | None:
        nonlocal recovery_lock_calls
        recovery_lock_calls += 1
        return await original_lock_by_user_id(lock_session, lock_user_id)

    monkeypatch.setattr(user_profile_repository, "add", duplicate_add)
    monkeypatch.setattr(
        user_profile_repository,
        "lock_by_user_id",
        tracking_lock_by_user_id,
    )

    try:
        async with factory() as setup_session:
            user = await _make_user(setup_session)
            await setup_session.commit()
            user_id = user.id

        async with factory() as request_session:
            async with api_client(
                request_session,
                _context(user_id),
            ) as client:
                response = await client.patch(
                    "/api/v1/me/profile",
                    json={"bio": "recovered update"},
                )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["bio"] == "recovered update"
        assert insert_attempts == 1
        assert winner_created is True
        assert recovery_lock_calls == 1

        async with factory() as verification_session:
            profiles = list(
                (
                    await verification_session.scalars(
                        select(UserProfile).where(UserProfile.user_id == user_id)
                    )
                ).all()
            )
        assert len(profiles) == 1
        assert profiles[0].bio == "recovered update"

        audit_rows = await _audit_rows(audit_session_factory)
        assert len(audit_rows) == 1
        assert audit_rows[0].resource_id == user_id
    finally:
        if user_id is not None:
            async with factory() as cleanup_session:
                await cleanup_session.execute(
                    delete(UserProfile).where(UserProfile.user_id == user_id)
                )
                await cleanup_session.execute(delete(User).where(User.id == user_id))
                await cleanup_session.commit()
        await engine.dispose()


async def test_put_privacy_updates_hidden_fields_and_edit_view_still_returns_values(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        signature="visible to profile owner",
        bio="also visible to profile owner",
        hidden_public_fields=["bio"],
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": ["avatar", "signature"]},
        )
        read_response = await client.get("/api/v1/me/profile")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["hidden_public_fields"] == [
        "signature",
        "avatar",
    ]
    assert read_response.status_code == 200, read_response.text
    read_data = read_response.json()["data"]
    assert read_data["hidden_public_fields"] == ["signature", "avatar"]
    assert read_data["signature"] == "visible to profile owner"
    assert read_data["bio"] == "also visible to profile owner"

    await session.refresh(profile)
    assert profile.hidden_public_fields == ["signature", "avatar"]


async def test_put_privacy_dedupes_and_uses_whitelist_order(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(user_id=user.id)
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": ["avatar", "bio", "avatar"]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["hidden_public_fields"] == ["bio", "avatar"]

    await session.refresh(profile)
    assert profile.hidden_public_fields == ["bio", "avatar"]


async def test_put_privacy_empty_list_clears_without_persisting_sql_null(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    profile = UserProfile(
        user_id=user.id,
        hidden_public_fields=["signature", "bio"],
    )
    session.add(profile)
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": []},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["hidden_public_fields"] == []

    await session.refresh(profile)
    assert profile.hidden_public_fields == []
    stored_value = await session.scalar(
        select(UserProfile.hidden_public_fields).where(
            UserProfile.user_id == user.id
        )
    )
    assert stored_value == []


@pytest.mark.parametrize(
    "hidden_field",
    ["not_a_profile_field", "nickname", "department_name"],
)
async def test_put_privacy_rejects_unknown_fields_before_writing(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
    hidden_field: str,
) -> None:
    user = await _make_user(session)

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": [hidden_field]},
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == 1006
    assert payload["message"] == "Unknown profile field"
    assert await _profile_for(session, user.id) is None
    assert await _audit_rows(audit_session_factory) == []


async def test_first_privacy_update_creates_missing_profile_row(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": ["work_phone"]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == user.id
    assert response.json()["data"]["hidden_public_fields"] == ["work_phone"]

    profile = await _profile_for(session, user.id)
    assert profile is not None
    assert profile.hidden_public_fields == ["work_phone"]
    assert profile.expertise_tags == []
    assert profile.personal_links == []


async def test_privacy_update_creates_one_audit_without_field_values(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _make_user(session)
    session.add(UserProfile(user_id=user.id))
    await session.flush()
    field_marker = "work_email"

    async with api_client(session, _context(user.id)) as client:
        response = await client.put(
            "/api/v1/me/profile/privacy",
            json={"hidden_fields": [field_marker]},
        )

    assert response.status_code == 200, response.text
    rows = await _audit_rows(audit_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_id == user.id
    assert row.action == "user_profile:privacy"
    assert row.resource_type == "user_profile"
    assert row.result == AuditResult.SUCCESS

    serialized = repr(
        {
            column.name: getattr(row, column.name)
            for column in AuditLog.__table__.columns
        }
    )
    assert field_marker not in serialized
