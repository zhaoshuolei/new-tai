import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings
from app.models.audit_log import AuditLog, AuditResult
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.models.system_metric import SystemMetric

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


async def test_audit_log_roundtrip(session: AsyncSession) -> None:
    row = AuditLog(
        user_id=1,
        actor_username="a",
        actor_nickname=None,
        action="user:update",
        resource_type="user",
        resource_id=2,
        result=AuditResult.DENIED,
        reason="FORBIDDEN",
        method="PATCH",
        path="/api/v1/users/2",
        ip="127.0.0.1",
        request_id="r1",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.result is AuditResult.DENIED
    assert row.request_id == "r1"


async def test_login_log_roundtrip(session: AsyncSession) -> None:
    row = LoginLog(
        user_id=None,
        username="alice",
        login_type=LoginType.LOCAL,
        result=LoginResultEnum.FAILURE,
        reason="INVALID_CREDENTIALS",
        ip="127.0.0.1",
        user_agent="pytest",
        device_id="device-1",
        request_id="r2",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.login_type is LoginType.LOCAL
    assert row.result is LoginResultEnum.FAILURE


async def test_system_metric_roundtrip(session: AsyncSession) -> None:
    sampled_at = datetime.now(UTC).replace(tzinfo=None)
    row = SystemMetric(
        cpu_percent=12.5,
        memory_percent=40.0,
        memory_used_mb=4096.0,
        memory_total_mb=8192.0,
        disk_percent=55.0,
        disk_used_mb=55_000.0,
        disk_total_mb=100_000.0,
        process_memory_mb=128.0,
        db_healthy=True,
        redis_healthy=True,
        sampled_at=sampled_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.cpu_percent == 12.5
    assert row.db_healthy is True
    assert row.sampled_at == sampled_at


async def test_exception_log_roundtrip(session: AsyncSession) -> None:
    occurred_at = datetime.now(UTC).replace(tzinfo=None)
    row = ExceptionLog(
        exception_type="ValueError",
        message="invalid value",
        fingerprint="fingerprint-1",
        level=ExceptionLevel.ERROR,
        request_id="r3",
        method="POST",
        path="/api/v1/users",
        user_id=1,
        status_code=500,
        occurred_at=occurred_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.level is ExceptionLevel.ERROR
    assert row.acknowledged is False
    assert row.acknowledged_by is None
    assert row.acknowledged_at is None


def test_log_model_indexes_and_foreign_keys() -> None:
    audit_table = cast(Table, AuditLog.__table__)
    login_table = cast(Table, LoginLog.__table__)
    exception_table = cast(Table, ExceptionLog.__table__)
    audit_indexes = {
        tuple(column.name for column in index.columns) for index in audit_table.indexes
    }
    assert ("user_id", "created_at") in audit_indexes
    assert ("resource_type", "resource_id") in audit_indexes
    assert ("action", "created_at") in audit_indexes
    assert any(index.name == "ix_audit_log_created_at" for index in audit_table.indexes)
    assert any(index.name == "ix_login_log_created_at" for index in login_table.indexes)
    assert SystemMetric.__table__.c.sampled_at.index is True
    assert ExceptionLog.__table__.c.occurred_at.index is True

    assert audit_table.c.user_id.foreign_keys == set()
    assert login_table.c.user_id.foreign_keys == set()
    assert exception_table.c.user_id.foreign_keys == set()


def test_settings_monitor_defaults(test_settings: Settings) -> None:
    assert test_settings.monitor_enabled is True
    assert test_settings.monitor_sample_interval_seconds == 60
    assert test_settings.monitor_metric_retention_days == 30
    assert test_settings.exception_log_retention_days == 90
    assert test_settings.login_log_retention_days == 180
    assert test_settings.audit_log_retention_days == 365
