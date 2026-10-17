import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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

from app.core.audit_meta import ActorSnapshot, get_audit_route_meta
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.models.system_setting import SystemSetting
from app.services import audit as audit_service

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00"


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "Tianxi Admin",
        "short_name": "Tianxi",
        "site_url": "https://admin.example.com",
        "logo_url": "/media/system/logo/logo.png",
        "favicon_url": "/media/system/favicon/favicon.ico",
        "description": "Operations console",
        "copyright": "Copyright 2026 Tianxi",
        "icp_number": "ICP-2026-0001",
    }
    values.update(overrides)
    return values


def _context(*permission_codes: str) -> AuthContext:
    return AuthContext(
        user_id=42,
        role_id=3,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
        username="settings-admin",
        nickname="Settings Admin",
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


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(root))
    return root


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


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
) -> list[AuditLog]:
    async with factory() as audit_session:
        rows = await audit_session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
        return list(rows.all())


async def test_public_settings_are_unauthenticated_and_omit_admin_metadata(
    session: AsyncSession,
) -> None:
    session.add(
        SystemSetting(
            setting_key="global",
            name="Public Tianxi",
            short_name="Tianxi",
            site_url="https://public.example.com",
            logo_url="/media/system/logo/public.png",
            favicon_url="/media/system/favicon/public.ico",
            description="Public description",
            copyright="Copyright 2026",
            icp_number="ICP-2026-0001",
            updated_by=99,
        )
    )
    await session.flush()

    async with api_client(session) as client:
        response = await client.get("/api/v1/system-settings/public")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "code": 0,
        "message": "ok",
        "data": {
            "name": "Public Tianxi",
            "short_name": "Tianxi",
            "site_url": "https://public.example.com",
            "logo_url": "/media/system/logo/public.png",
            "favicon_url": "/media/system/favicon/public.ico",
            "description": "Public description",
            "copyright": "Copyright 2026",
            "icp_number": "ICP-2026-0001",
        },
    }


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/v1/system-settings", {}),
        ("put", "/api/v1/system-settings", {"json": _payload()}),
        (
            "post",
            "/api/v1/system-settings/assets/logo",
            {"content": PNG, "headers": {"Content-Type": "image/png"}},
        ),
    ],
)
async def test_protected_settings_routes_return_403_without_permission(
    session: AsyncSession,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    async with api_client(session, _context()) as client:
        response = await getattr(client, method)(path, **kwargs)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == 1010


async def test_admin_update_replaces_all_fields_and_records_audit(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = _context("system_setting:read", "system_setting:update")

    async with api_client(session, context) as client:
        first = await client.put(
            "/api/v1/system-settings",
            json=_payload(name="  Tianxi Admin  "),
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["name"] == "Tianxi Admin"
        assert first.json()["data"]["updated_by"] == context.user_id

        second = await client.put(
            "/api/v1/system-settings",
            json={
                "name": "Minimal Settings",
                "site_url": "https://minimal.example.com/path",
            },
        )
        assert second.status_code == 200, second.text

        read = await client.get("/api/v1/system-settings")

    assert read.status_code == 200, read.text
    data = read.json()["data"]
    assert data["name"] == "Minimal Settings"
    assert data["short_name"] is None
    assert data["site_url"] == "https://minimal.example.com/path"
    assert data["logo_url"] is None
    assert data["favicon_url"] is None
    assert data["description"] is None
    assert data["copyright"] is None
    assert data["icp_number"] is None
    assert data["updated_at"] is not None
    assert data["updated_by"] == context.user_id

    stored = await session.scalar(
        select(SystemSetting).where(SystemSetting.setting_key == "global")
    )
    assert stored is not None

    rows = await _audit_rows(
        audit_session_factory,
        action="system_setting:update",
    )
    assert len(rows) == 2
    assert all(row.result is AuditResult.SUCCESS for row in rows)
    assert all(row.resource_type == "system_setting" for row in rows)
    assert all(row.resource_id == stored.id for row in rows)
    assert all(row.user_id == context.user_id for row in rows)
    assert all(row.actor_username == context.username for row in rows)

    meta = get_audit_route_meta("PUT", "/system-settings")
    assert meta is not None
    assert meta.action == "system_setting:update"
    assert meta.resource_type == "system_setting"


async def test_admin_read_requires_system_setting_read(
    session: AsyncSession,
) -> None:
    async with api_client(session, _context("system_setting:update")) as client:
        response = await client.get("/api/v1/system-settings")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == 1010


async def test_permission_denied_put_records_one_denied_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = _context("system_setting:read")

    async with api_client(session, context) as client:
        response = await client.put(
            "/api/v1/system-settings",
            json=_payload(),
        )

    assert response.status_code == 403, response.text
    rows = await _audit_rows(
        audit_session_factory,
        action="system_setting:update",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.result is AuditResult.DENIED
    assert row.action == "system_setting:update"
    assert row.resource_type == "system_setting"
    assert row.reason == "FORBIDDEN"
    assert row.user_id == context.user_id
    assert row.actor_username == context.username
    assert row.actor_nickname == context.nickname


async def test_permission_denied_upload_records_one_denied_audit_row(
    session: AsyncSession,
    media_root: Path,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = _context("system_setting:read")

    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/system-settings/assets/logo",
            content=PNG,
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 403, response.text
    assert not media_root.exists() or not any(media_root.rglob("*"))
    rows = await _audit_rows(
        audit_session_factory,
        action="system_setting:asset_upload",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.result is AuditResult.DENIED
    assert row.resource_type == "system_setting"
    assert row.reason == "FORBIDDEN"
    assert row.user_id == context.user_id
    assert row.actor_username == context.username
    assert row.actor_nickname == context.nickname


async def test_valid_upload_returns_url_writes_file_and_records_audit(
    session: AsyncSession,
    media_root: Path,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    context = _context("system_setting:update")

    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/system-settings/assets/logo",
            content=PNG,
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 200, response.text
    url = response.json()["data"]["url"]
    assert response.json()["data"] == {"url": url}
    assert url.startswith("/media/system/logo/")
    assert url.endswith(".png")
    assert (media_root / "system" / "logo" / Path(url).name).read_bytes() == PNG

    rows = await _audit_rows(
        audit_session_factory,
        action="system_setting:asset_upload",
    )
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "system_setting"
    assert rows[0].resource_id is None
    assert rows[0].user_id == context.user_id
    assert rows[0].actor_username == context.username


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (b"not-an-image", "image/png"),
        (PNG, "image/jpeg"),
    ],
)
async def test_rejected_upload_persists_no_file_or_audit_row(
    session: AsyncSession,
    media_root: Path,
    audit_session_factory: async_sessionmaker[AsyncSession],
    payload: bytes,
    content_type: str,
) -> None:
    async with api_client(session, _context("system_setting:update")) as client:
        response = await client.post(
            "/api/v1/system-settings/assets/favicon",
            content=payload,
            headers={"Content-Type": content_type},
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == 1006
    assert not media_root.exists() or not any(media_root.rglob("*"))
    assert (
        await _audit_rows(
            audit_session_factory,
            action="system_setting:asset_upload",
        )
        == []
    )


async def test_oversized_upload_is_rejected_without_persisting_file(
    session: AsyncSession,
    media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "system_asset_max_bytes", len(PNG))

    async with api_client(session, _context("system_setting:update")) as client:
        response = await client.post(
            "/api/v1/system-settings/assets/logo",
            content=PNG + b"\x00",
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == 1006
    assert not media_root.exists() or not any(media_root.rglob("*"))


async def test_unsupported_asset_type_is_rejected(
    session: AsyncSession,
    media_root: Path,
) -> None:
    async with api_client(session, _context("system_setting:update")) as client:
        response = await client.post(
            "/api/v1/system-settings/assets/svg",
            content=b"<svg/>",
            headers={"Content-Type": "image/svg+xml"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == 1006
    assert not media_root.exists() or not any(media_root.rglob("*"))
