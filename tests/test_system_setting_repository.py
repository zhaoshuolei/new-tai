import os
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.system_setting import SystemSetting
from app.repositories import system_setting as system_setting_repository
from app.repositories.system_setting import get_global, upsert_global

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


def _values(*, name: str = "天曦管理后台") -> dict[str, object]:
    return {
        "name": name,
        "short_name": "天曦",
        "site_url": "https://admin.example.com",
        "logo_url": "/media/system/logo/logo.png",
        "favicon_url": "/media/system/favicon/favicon.ico",
        "description": "统一运营管理平台",
        "copyright": "Copyright 2026 Tianxi",
        "icp_number": "京ICP备2026000001号",
    }


async def test_get_global_returns_none_when_not_configured(
    session: AsyncSession,
) -> None:
    assert await get_global(session) is None


async def test_upsert_creates_then_updates_single_row(session: AsyncSession) -> None:
    first = await upsert_global(session, _values(), updated_by=11)

    assert isinstance(first.created_at, datetime)
    assert isinstance(first.updated_at, datetime)
    assert first.setting_key == "global"
    assert first.name == "天曦管理后台"
    assert first.site_url == "https://admin.example.com"

    second = await upsert_global(session, _values(name="新站名"), updated_by=22)

    assert second.id == first.id
    assert second.name == "新站名"
    assert second.updated_by == 22
    row_count = await session.scalar(select(func.count()).select_from(SystemSetting))
    assert row_count == 1


async def test_upsert_recovers_when_concurrent_insert_is_not_visible(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await upsert_global(session, _values(), updated_by=11)

    async def lookup_sees_no_row(_session: AsyncSession) -> None:
        return None

    monkeypatch.setattr(system_setting_repository, "get_global", lookup_sees_no_row)

    updated = await upsert_global(
        session,
        _values(name="并发写入后的名称"),
        updated_by=22,
    )

    rows = list((await session.scalars(select(SystemSetting))).all())
    assert len(rows) == 1
    assert rows[0].id == existing.id
    assert updated.id == existing.id
    assert rows[0].name == "并发写入后的名称"
    assert rows[0].updated_by == 22
