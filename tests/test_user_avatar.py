"""Current-user avatar API tests."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile
from app.services import audit as audit_service
from tests.conftest import FakeRedis
from tests.test_system_setting_asset import ICO, JPEG, PNG, WEBP

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


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(root))
    return root


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
    redis: FakeRedis,
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
    application.dependency_overrides[get_redis] = lambda: redis

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
        username=f"avatar-owner-{user_id}",
        nickname="Avatar Owner",
    )


async def _make_user(
    session: AsyncSession,
    *,
    avatar: str | None = None,
) -> User:
    user = User(
        feishu_open_id=f"ou_{uuid4().hex}",
        username=f"avatar-api-{uuid4().hex[:12]}",
        nickname="Avatar Owner",
        email="avatar@example.com",
        avatar=avatar,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def _cleanup_user(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(UserProfile).where(UserProfile.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as audit_session:
        rows = await audit_session.scalars(select(AuditLog).order_by(AuditLog.id))
        return list(rows.all())


async def test_upload_png_stores_uuid_file_and_updates_user(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG,
                headers={"Content-Type": "image/png"},
            )

        assert response.status_code == 200, response.text
        url = response.json()["data"]["url"]
        assert url.startswith("/media/user/avatar/")
        filename = url.removeprefix("/media/user/avatar/")
        assert filename.endswith(".png")
        UUID(Path(filename).stem)
        assert (media_root / "user" / "avatar" / filename).read_bytes() == PNG

        await session.refresh(user)
        assert user.avatar == url
    finally:
        await _cleanup_user(session, user.id)


async def test_upload_without_content_type_is_allowed(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=WEBP,
            )

        assert response.status_code == 200, response.text
        url = response.json()["data"]["url"]
        assert url.endswith(".webp")
        assert (media_root / "user" / "avatar" / Path(url).name).read_bytes() == WEBP
    finally:
        await _cleanup_user(session, user.id)


async def test_declared_content_type_must_match_png_bytes(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session, avatar="https://example.com/current.png")
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG,
                headers={"Content-Type": "image/jpeg"},
            )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == 1006
        assert _files(media_root) == []
        await session.refresh(user)
        assert user.avatar == "https://example.com/current.png"
    finally:
        await _cleanup_user(session, user.id)


@pytest.mark.parametrize("payload", [b"not-an-image", JPEG + b"broken"])
async def test_unsupported_image_bytes_are_rejected_without_files(
    payload: bytes,
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=payload,
                headers={"Content-Type": "image/png"},
            )

        assert response.status_code == 422, response.text
        assert _files(media_root) == []
        await session.refresh(user)
        assert user.avatar is None
    finally:
        await _cleanup_user(session, user.id)


async def test_body_over_configured_maximum_is_rejected_without_files(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _make_user(session)
    monkeypatch.setattr(settings, "system_asset_max_bytes", len(PNG))
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG + b"\x00",
                headers={"Content-Type": "image/png"},
            )

        assert response.status_code == 422, response.text
        assert _files(media_root) == []
        await session.refresh(user)
        assert user.avatar is None
    finally:
        await _cleanup_user(session, user.id)


async def test_replacing_managed_avatar_removes_old_file_after_commit(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    old_name = f"{UUID(int=7)}.png"
    old_path = media_root / "user" / "avatar" / old_name
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(ICO)
    user = await _make_user(session, avatar=f"/media/user/avatar/{old_name}")
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG,
                headers={"Content-Type": "image/png"},
            )

        assert response.status_code == 200, response.text
        new_url = response.json()["data"]["url"]
        new_path = media_root / "user" / "avatar" / Path(new_url).name
        assert new_url != f"/media/user/avatar/{old_name}"
        assert old_path.exists() is False
        assert new_path.read_bytes() == PNG

        await session.refresh(user)
        assert user.avatar == new_url
    finally:
        await _cleanup_user(session, user.id)


async def test_external_avatar_is_left_alone_when_replaced(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    external_url = "https://example.com/a.png"
    user = await _make_user(session, avatar=external_url)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            response = await client.post(
                "/api/v1/me/profile/avatar",
                content=JPEG,
                headers={"Content-Type": "image/jpeg"},
            )

        assert response.status_code == 200, response.text
        new_url = response.json()["data"]["url"]
        assert new_url.startswith("/media/user/avatar/")
        assert (media_root / "user" / "avatar" / Path(new_url).name).exists()
        await session.refresh(user)
        assert user.avatar == new_url
    finally:
        await _cleanup_user(session, user.id)


async def test_delete_clears_avatar_and_removes_managed_file(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            upload_response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG,
                headers={"Content-Type": "image/png"},
            )
            assert upload_response.status_code == 200, upload_response.text
            uploaded_url = upload_response.json()["data"]["url"]
            uploaded_path = media_root / "user" / "avatar" / Path(uploaded_url).name
            assert uploaded_path.exists()

            response = await client.delete("/api/v1/me/profile/avatar")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["url"] is None
        assert uploaded_path.exists() is False
        await session.refresh(user)
        assert user.avatar is None
    finally:
        await _cleanup_user(session, user.id)


async def test_eleventh_avatar_upload_is_rate_limited_before_file_access(
    session: AsyncSession,
    fake_redis: FakeRedis,
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            responses = []
            for _ in range(11):
                responses.append(
                    await client.post(
                        "/api/v1/me/profile/avatar",
                        content=PNG,
                        headers={"Content-Type": "image/png"},
                    )
                )

        assert [response.status_code for response in responses[:10]] == [200] * 10
        assert responses[10].status_code == 429, responses[10].text
        assert responses[10].json()["code"] == 1005
        assert len(_files(media_root)) == 1
        assert len(fake_redis.values) == 1
        assert next(iter(fake_redis.values.values())) == "11"
        assert next(iter(fake_redis.expirations.values())) == 3600
    finally:
        await _cleanup_user(session, user.id)


async def test_upload_and_delete_each_record_one_audit_with_resource_id(
    session: AsyncSession,
    fake_redis: FakeRedis,
    audit_session_factory: async_sessionmaker[AsyncSession],
    media_root: Path,
) -> None:
    user = await _make_user(session)
    try:
        async with api_client(session, _context(user.id), fake_redis) as client:
            upload_response = await client.post(
                "/api/v1/me/profile/avatar",
                content=PNG,
                headers={"Content-Type": "image/png"},
            )
            assert upload_response.status_code == 200, upload_response.text
            delete_response = await client.delete("/api/v1/me/profile/avatar")
            assert delete_response.status_code == 200, delete_response.text

        rows = await _audit_rows(audit_session_factory)
        assert [row.action for row in rows] == [
            "user_profile:avatar_upload",
            "user_profile:avatar_delete",
        ]
        assert all(row.resource_type == "user_profile" for row in rows)
        assert all(row.resource_id == user.id for row in rows)
        assert all(row.result == AuditResult.SUCCESS for row in rows)
    finally:
        await _cleanup_user(session, user.id)
