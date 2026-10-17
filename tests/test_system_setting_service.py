import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.db.session import session_factory as application_session_factory
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.repositories.system_setting import get_global
from app.schemas.system_setting import (
    PublicSystemSettingRead,
    SystemSettingRead,
    SystemSettingUpdate,
)
from app.services import audit as audit_service
from app.services.system_setting import (
    get_admin_setting,
    get_public_setting,
    update_setting,
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


@pytest_asyncio.fixture(autouse=True)
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(
                delete(AuditLog).where(AuditLog.action == "system_setting:update")
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest.fixture
def context() -> AuthContext:
    return AuthContext(
        user_id=42,
        role_id=3,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset({"system_setting:update"}),
        is_super_admin=False,
        username="settings-admin",
        nickname="Settings Admin",
    )


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


async def test_audited_service_writes_are_isolated_from_test_transaction(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    assert audit_service.session_factory is not application_session_factory

    await update_setting(
        session,
        SystemSettingUpdate.model_validate(_payload()),
        context=context,
    )

    async with audit_service.session_factory() as audit_session:
        isolated_rows = (
            await audit_session.scalars(
                select(AuditLog).where(AuditLog.action == "system_setting:update")
            )
        ).all()
    transaction_rows = (
        await session.scalars(
            select(AuditLog).where(AuditLog.action == "system_setting:update")
        )
    ).all()

    assert len(isolated_rows) == 1
    assert transaction_rows == []


def test_update_schema_trims_values_and_normalizes_blank_optionals() -> None:
    payload = SystemSettingUpdate(
        name="  Tianxi Admin  ",
        short_name="  ",
        site_url="  https://admin.example.com/path?from=test  ",
        logo_url="  /media/system/logo/logo.png  ",
        favicon_url="\t",
        description="  Operations console  ",
        copyright="  Copyright 2026 Tianxi  ",
        icp_number="  ICP-2026-0001  ",
    )

    assert payload.model_dump() == {
        "name": "Tianxi Admin",
        "short_name": None,
        "site_url": "https://admin.example.com/path?from=test",
        "logo_url": "/media/system/logo/logo.png",
        "favicon_url": None,
        "description": "Operations console",
        "copyright": "Copyright 2026 Tianxi",
        "icp_number": "ICP-2026-0001",
    }


def test_update_schema_requires_name_and_site_url() -> None:
    with pytest.raises(ValidationError) as name_error:
        SystemSettingUpdate.model_validate({"site_url": "https://example.com"})
    assert name_error.value.errors()[0]["loc"] == ("name",)

    with pytest.raises(ValidationError) as url_error:
        SystemSettingUpdate.model_validate({"name": "Tianxi Admin"})
    assert url_error.value.errors()[0]["loc"] == ("site_url",)


@pytest.mark.parametrize("name", ["", "   "])
def test_update_schema_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate(name=name, site_url="https://example.com")
    assert error.value.errors()[0]["loc"] == ("name",)


def test_update_schema_enforces_length_boundaries() -> None:
    payload = SystemSettingUpdate(
        name="n" * 100,
        short_name="s" * 50,
        site_url=f"https://example.com/{'p' * 480}",
        logo_url=f"/{'l' * 499}",
        favicon_url=f"https://example.com/{'f' * 480}",
        description="d" * 500,
        copyright="c" * 200,
        icp_number="i" * 100,
    )
    assert len(payload.name) == 100
    assert len(payload.description or "") == 500

    oversized = {
        "name": "n" * 101,
        "short_name": "s" * 51,
        "site_url": f"https://example.com/{'p' * 481}",
        "logo_url": f"/{'l' * 500}",
        "favicon_url": f"https://example.com/{'f' * 481}",
        "description": "d" * 501,
        "copyright": "c" * 201,
        "icp_number": "i" * 101,
    }
    for field_name, value in oversized.items():
        with pytest.raises(ValidationError) as error:
            SystemSettingUpdate.model_validate(_payload(**{field_name: value}))
        assert error.value.errors()[0]["loc"] == (field_name,)


@pytest.mark.parametrize(
    "site_url",
    [
        "http://example.com",
        "https://example.com",
        "https://example.com/path/to/site?source=test#section",
    ],
)
def test_update_schema_accepts_absolute_http_site_urls(site_url: str) -> None:
    assert SystemSettingUpdate(name="Tianxi", site_url=site_url).site_url == site_url


@pytest.mark.parametrize(
    "site_url",
    [
        "/relative/path",
        "ftp://example.com",
        "mailto:admin@example.com",
        "https://user:secret@example.com",
        "https://user@example.com",
        "https://example.com:99999",
    ],
)
def test_update_schema_rejects_invalid_site_urls(site_url: str) -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate(name="Tianxi", site_url=site_url)
    assert error.value.errors()[0]["loc"] == ("site_url",)


@pytest.mark.parametrize(
    "site_url",
    [
        "https://example.com/path\x00suffix",
        "https://example.com/path\x1fsuffix",
        "https://example.com/path\x7fsuffix",
        "https://example.com/path\x85suffix",
        "https://example.com/path\u200bsuffix",
        "https://example.com\\path",
        "https://example.com/path\\suffix",
    ],
)
def test_update_schema_rejects_controls_del_and_backslashes_in_site_urls(
    site_url: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate(name="Tianxi", site_url=site_url)
    assert error.value.errors()[0]["loc"] == ("site_url",)


@pytest.mark.parametrize(
    "asset_url",
    [
        "/media/system/logo/logo.png",
        "http://cdn.example.com/logo.png",
        "https://cdn.example.com/logo.png?v=1",
    ],
)
def test_update_schema_accepts_relative_and_absolute_asset_urls(
    asset_url: str,
) -> None:
    payload = SystemSettingUpdate(
        name="Tianxi",
        site_url="https://example.com",
        logo_url=asset_url,
        favicon_url=asset_url,
    )
    assert payload.logo_url == asset_url
    assert payload.favicon_url == asset_url


@pytest.mark.parametrize(
    "asset_url",
    [
        "media/logo.png",
        "//cdn.example.com/logo.png",
        "ftp://cdn.example.com/logo.png",
        "https://user:secret@cdn.example.com/logo.png",
    ],
)
@pytest.mark.parametrize("field_name", ["logo_url", "favicon_url"])
def test_update_schema_rejects_invalid_asset_urls(
    field_name: str,
    asset_url: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate.model_validate(_payload(**{field_name: asset_url}))
    assert error.value.errors()[0]["loc"] == (field_name,)


@pytest.mark.parametrize(
    "asset_url",
    [
        "/media/system/logo/\x00logo.png",
        "/media/system/logo/\x1flogo.png",
        "/media/system/logo/logo\x7f.png",
        "/media/system/logo/logo\x85.png",
        "/media/system/logo/logo\u200b.png",
        "/\\evil.example/logo.png",
        "\\host\\path",
        "/media\\system\\logo.png",
        "https://cdn.example.com\\logo.png",
        "///cdn.example.com/logo.png",
    ],
)
@pytest.mark.parametrize("field_name", ["logo_url", "favicon_url"])
def test_update_schema_rejects_controls_del_and_backslashes_in_asset_urls(
    field_name: str,
    asset_url: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate.model_validate(_payload(**{field_name: asset_url}))
    assert error.value.errors()[0]["loc"] == (field_name,)


def test_update_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError) as error:
        SystemSettingUpdate.model_validate(_payload(unexpected="value"))
    assert error.value.errors()[0]["type"] == "extra_forbidden"


async def test_reads_return_defaults_before_first_save(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_name", "Configured App Name")

    public = await get_public_setting(session)
    admin = await get_admin_setting(session)

    assert public == PublicSystemSettingRead(
        name="Configured App Name",
        short_name=None,
        site_url=None,
        logo_url=None,
        favicon_url=None,
        description=None,
        copyright=None,
        icp_number=None,
    )
    assert admin == SystemSettingRead(
        name="Configured App Name",
        short_name=None,
        site_url=None,
        logo_url=None,
        favicon_url=None,
        description=None,
        copyright=None,
        icp_number=None,
        updated_at=None,
        updated_by=None,
    )


async def test_update_setting_persists_all_fields_and_updater(
    session: AsyncSession,
    context: AuthContext,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await update_setting(
        session,
        SystemSettingUpdate.model_validate(_payload(name="  Tianxi Admin  ")),
        context=context,
    )

    stored = await get_global(session)
    assert stored is not None
    assert result is stored
    assert stored.name == "Tianxi Admin"
    assert stored.short_name == "Tianxi"
    assert stored.site_url == "https://admin.example.com"
    assert stored.logo_url == "/media/system/logo/logo.png"
    assert stored.favicon_url == "/media/system/favicon/favicon.ico"
    assert stored.description == "Operations console"
    assert stored.copyright == "Copyright 2026 Tianxi"
    assert stored.icp_number == "ICP-2026-0001"
    assert stored.updated_by == context.user_id
    assert isinstance(stored.updated_at, datetime)

    async with audit_session_factory() as audit_session:
        rows = (
            await audit_session.scalars(
                select(AuditLog).where(AuditLog.action == "system_setting:update")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].result is AuditResult.SUCCESS
    assert rows[0].resource_type == "system_setting"
    assert rows[0].resource_id == stored.id
    assert rows[0].user_id == context.user_id
    assert rows[0].actor_username == context.username
    assert rows[0].actor_nickname == context.nickname


async def test_update_setting_removes_replaced_managed_assets(
    session: AsyncSession,
    context: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(media_root))
    old_logo = f"/media/system/logo/{UUID(int=1)}.png"
    new_logo = f"/media/system/logo/{UUID(int=2)}.png"
    old_path = media_root / "system" / "logo" / Path(old_logo).name
    new_path = media_root / "system" / "logo" / Path(new_logo).name
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"old")
    new_path.write_bytes(b"new")

    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=old_logo, favicon_url="https://cdn.example.com/icon.ico")
        ),
        context=context,
    )
    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=new_logo, favicon_url=None)
        ),
        context=context,
    )

    assert old_path.exists() is False
    assert new_path.exists() is True


async def test_update_setting_keeps_asset_shared_by_logo_and_favicon(
    session: AsyncSession,
    context: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(media_root))
    shared_url = f"/media/system/logo/{UUID(int=4)}.png"
    new_logo = f"/media/system/logo/{UUID(int=5)}.png"
    shared_path = media_root / "system" / "logo" / Path(shared_url).name
    shared_path.parent.mkdir(parents=True)
    shared_path.write_bytes(b"shared")

    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=shared_url, favicon_url=shared_url)
        ),
        context=context,
    )
    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=new_logo, favicon_url=shared_url)
        ),
        context=context,
    )

    assert shared_path.exists() is True


async def test_update_setting_keeps_assets_whose_urls_are_swapped(
    session: AsyncSession,
    context: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(media_root))
    first_url = f"/media/system/logo/{UUID(int=6)}.png"
    second_url = f"/media/system/logo/{UUID(int=7)}.png"
    first_path = media_root / "system" / "logo" / Path(first_url).name
    second_path = media_root / "system" / "logo" / Path(second_url).name
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")

    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=first_url, favicon_url=second_url)
        ),
        context=context,
    )
    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=second_url, favicon_url=first_url)
        ),
        context=context,
    )

    assert first_path.exists() is True
    assert second_path.exists() is True


async def test_update_setting_keeps_unchanged_managed_asset(
    session: AsyncSession,
    context: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(media_root))
    logo_url = f"/media/system/logo/{UUID(int=3)}.png"
    logo_path = media_root / "system" / "logo" / Path(logo_url).name
    logo_path.parent.mkdir(parents=True)
    logo_path.write_bytes(b"logo")

    await update_setting(
        session,
        SystemSettingUpdate.model_validate(_payload(logo_url=logo_url)),
        context=context,
    )
    await update_setting(
        session,
        SystemSettingUpdate.model_validate(
            _payload(logo_url=logo_url, name="Renamed Site")
        ),
        context=context,
    )

    assert logo_path.exists() is True


async def test_update_setting_omitted_optionals_clear_existing_values(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    await update_setting(
        session,
        SystemSettingUpdate.model_validate(_payload()),
        context=context,
    )

    updated = await update_setting(
        session,
        SystemSettingUpdate(
            name="Minimal Settings",
            site_url="https://minimal.example.com/path",
        ),
        context=context,
    )

    assert updated.name == "Minimal Settings"
    assert updated.site_url == "https://minimal.example.com/path"
    assert updated.short_name is None
    assert updated.logo_url is None
    assert updated.favicon_url is None
    assert updated.description is None
    assert updated.copyright is None
    assert updated.icp_number is None


async def test_update_setting_rejects_defensively_blank_name(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    payload = SystemSettingUpdate.model_construct(
        name="   ",
        site_url="https://example.com",
    )

    with pytest.raises(BusinessError) as error:
        await update_setting(session, payload, context=context)

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert await get_global(session) is None
