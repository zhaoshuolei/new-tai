import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.audit_log import AuditLog, AuditResult
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.models.system_metric import SystemMetric
from app.services.monitor import run_retention

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
_RETENTION_TABLES = (SystemMetric, ExceptionLog, AuditLog, LoginLog)


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


def _retention_rows(
    now: datetime,
) -> list[tuple[str, str, type[Any], Any, Any]]:
    expired = now - timedelta(days=10)

    return [
        (
            "system_metric",
            "monitor_metric_retention_days",
            SystemMetric,
            SystemMetric(
                cpu_percent=10.0,
                memory_percent=20.0,
                memory_used_mb=30.0,
                memory_total_mb=40.0,
                disk_percent=50.0,
                disk_used_mb=60.0,
                disk_total_mb=70.0,
                process_memory_mb=80.0,
                db_healthy=True,
                redis_healthy=True,
                sampled_at=expired,
            ),
            SystemMetric(
                cpu_percent=11.0,
                memory_percent=21.0,
                memory_used_mb=31.0,
                memory_total_mb=41.0,
                disk_percent=51.0,
                disk_used_mb=61.0,
                disk_total_mb=71.0,
                process_memory_mb=81.0,
                db_healthy=True,
                redis_healthy=True,
                sampled_at=now,
            ),
        ),
        (
            "exception_log",
            "exception_log_retention_days",
            ExceptionLog,
            ExceptionLog(
                exception_type="RetentionError",
                message="expired",
                fingerprint="retention-expired",
                level=ExceptionLevel.ERROR,
                request_id="retention-exception-expired",
                method="GET",
                path="/retention",
                user_id=None,
                status_code=500,
                occurred_at=expired,
            ),
            ExceptionLog(
                exception_type="RetentionError",
                message="current",
                fingerprint="retention-current",
                level=ExceptionLevel.ERROR,
                request_id="retention-exception-current",
                method="GET",
                path="/retention",
                user_id=None,
                status_code=500,
                occurred_at=now,
            ),
        ),
        (
            "audit_log",
            "audit_log_retention_days",
            AuditLog,
            AuditLog(
                user_id=None,
                actor_username="retention",
                actor_nickname=None,
                action="retention:test",
                resource_type="retention",
                resource_id=None,
                result=AuditResult.SUCCESS,
                reason=None,
                method="POST",
                path="/retention",
                ip=None,
                request_id="retention-audit-expired",
                created_at=expired,
            ),
            AuditLog(
                user_id=None,
                actor_username="retention",
                actor_nickname=None,
                action="retention:test",
                resource_type="retention",
                resource_id=None,
                result=AuditResult.SUCCESS,
                reason=None,
                method="POST",
                path="/retention",
                ip=None,
                request_id="retention-audit-current",
                created_at=now,
            ),
        ),
        (
            "login_log",
            "login_log_retention_days",
            LoginLog,
            LoginLog(
                user_id=None,
                username="retention",
                login_type=LoginType.LOCAL,
                result=LoginResultEnum.FAILURE,
                reason="RETENTION_TEST",
                ip=None,
                user_agent=None,
                device_id=None,
                request_id="retention-login-expired",
                created_at=expired,
            ),
            LoginLog(
                user_id=None,
                username="retention",
                login_type=LoginType.LOCAL,
                result=LoginResultEnum.FAILURE,
                reason="RETENTION_TEST",
                ip=None,
                user_agent=None,
                device_id=None,
                request_id="retention-login-current",
                created_at=now,
            ),
        ),
    ]


async def _seed_rows(
    session: AsyncSession,
    rows: list[tuple[str, str, type[Any], Any, Any]],
) -> None:
    for model in _RETENTION_TABLES:
        await session.execute(delete(model))
    for _, _, _, old_row, new_row in rows:
        session.add_all((old_row, new_row))
    await session.commit()


async def test_retention_deletes_only_expired(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = _retention_rows(now)
    await _seed_rows(session, rows)
    for _, setting_name, _, _, _ in rows:
        monkeypatch.setattr(settings, setting_name, 7)

    deleted = await run_retention(session)

    assert deleted == {
        "system_metric": 1,
        "exception_log": 1,
        "audit_log": 1,
        "login_log": 1,
    }
    for table_name, _, model, old_row, new_row in rows:
        persisted_ids = set((await session.scalars(select(model.id))).all())
        assert old_row.id not in persisted_ids, table_name
        assert new_row.id in persisted_ids, table_name


@pytest.mark.parametrize("disabled_index", range(len(_RETENTION_TABLES)))
async def test_retention_zero_disables_cleanup(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    disabled_index: int,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = _retention_rows(now)
    await _seed_rows(session, rows)
    for _, setting_name, _, _, _ in rows:
        monkeypatch.setattr(settings, setting_name, 7)

    table_name, setting_name, model, old_row, new_row = rows[disabled_index]
    monkeypatch.setattr(settings, setting_name, 0)

    deleted = await run_retention(session)

    assert deleted[table_name] == 0
    assert await session.get(model, old_row.id) is not None
    assert await session.get(model, new_row.id) is not None
