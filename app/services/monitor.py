"""系统监控采样领域业务逻辑。"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import psutil
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import redis_client
from app.db.session import session_factory
from app.models.audit_log import AuditLog
from app.models.exception_log import ExceptionLog
from app.models.login_log import LoginLog
from app.models.system_metric import SystemMetric
from app.repositories import system_metric as system_metric_repository
from app.services.health import check_database, check_redis

_BYTES_PER_MB = 1024 * 1024
_RETENTION_INTERVAL_SECONDS = 60 * 60
logger = logging.getLogger(__name__)


class PsutilSnapshot(TypedDict):
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_mb: float
    disk_total_mb: float
    process_memory_mb: float


class LiveSnapshot(PsutilSnapshot):
    db_healthy: bool
    redis_healthy: bool
    sampled_at: datetime


def collect_psutil_snapshot() -> PsutilSnapshot:
    """Collect the current host and process resource snapshot."""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    process = psutil.Process()
    return PsutilSnapshot(
        cpu_percent=psutil.cpu_percent(interval=0.1),
        memory_percent=memory.percent,
        memory_used_mb=memory.used / _BYTES_PER_MB,
        memory_total_mb=memory.total / _BYTES_PER_MB,
        disk_percent=disk.percent,
        disk_used_mb=disk.used / _BYTES_PER_MB,
        disk_total_mb=disk.total / _BYTES_PER_MB,
        process_memory_mb=process.memory_info().rss / _BYTES_PER_MB,
    )


async def collect_live_snapshot(
    session: AsyncSession,
    redis: Redis,
) -> LiveSnapshot:
    """Collect a live resource snapshot and both application probes."""
    snapshot = collect_psutil_snapshot()
    return LiveSnapshot(
        cpu_percent=snapshot["cpu_percent"],
        memory_percent=snapshot["memory_percent"],
        memory_used_mb=snapshot["memory_used_mb"],
        memory_total_mb=snapshot["memory_total_mb"],
        disk_percent=snapshot["disk_percent"],
        disk_used_mb=snapshot["disk_used_mb"],
        disk_total_mb=snapshot["disk_total_mb"],
        process_memory_mb=snapshot["process_memory_mb"],
        db_healthy=await check_database(session),
        redis_healthy=await check_redis(redis),
        sampled_at=datetime.now(UTC).replace(tzinfo=None),
    )


async def sample_once(session: AsyncSession) -> SystemMetric:
    """Collect, probe, and persist one system metric row."""
    snapshot = await collect_live_snapshot(session, redis_client)
    row = SystemMetric(
        cpu_percent=snapshot["cpu_percent"],
        memory_percent=snapshot["memory_percent"],
        memory_used_mb=snapshot["memory_used_mb"],
        memory_total_mb=snapshot["memory_total_mb"],
        disk_percent=snapshot["disk_percent"],
        disk_used_mb=snapshot["disk_used_mb"],
        disk_total_mb=snapshot["disk_total_mb"],
        process_memory_mb=snapshot["process_memory_mb"],
        db_healthy=snapshot["db_healthy"],
        redis_healthy=snapshot["redis_healthy"],
        sampled_at=snapshot["sampled_at"],
    )
    await system_metric_repository.add(session, row)
    await session.commit()
    await session.refresh(row)
    return row


async def run_retention(session: AsyncSession) -> dict[str, int]:
    """Delete expired monitoring and log rows using each table's retention rule."""
    now = datetime.now(UTC).replace(tzinfo=None)
    deleted = {
        "system_metric": 0,
        "exception_log": 0,
        "audit_log": 0,
        "login_log": 0,
    }

    if settings.monitor_metric_retention_days > 0:
        cutoff = now - timedelta(days=settings.monitor_metric_retention_days)
        result = await session.execute(
            delete(SystemMetric).where(SystemMetric.sampled_at < cutoff)
        )
        deleted["system_metric"] = int(getattr(result, "rowcount", 0) or 0)

    if settings.exception_log_retention_days > 0:
        cutoff = now - timedelta(days=settings.exception_log_retention_days)
        result = await session.execute(
            delete(ExceptionLog).where(ExceptionLog.occurred_at < cutoff)
        )
        deleted["exception_log"] = int(getattr(result, "rowcount", 0) or 0)

    if settings.audit_log_retention_days > 0:
        cutoff = now - timedelta(days=settings.audit_log_retention_days)
        result = await session.execute(
            delete(AuditLog).where(AuditLog.created_at < cutoff)
        )
        deleted["audit_log"] = int(getattr(result, "rowcount", 0) or 0)

    if settings.login_log_retention_days > 0:
        cutoff = now - timedelta(days=settings.login_log_retention_days)
        result = await session.execute(
            delete(LoginLog).where(LoginLog.created_at < cutoff)
        )
        deleted["login_log"] = int(getattr(result, "rowcount", 0) or 0)

    if any(deleted.values()):
        await session.commit()
    return deleted


async def run_sampler(interval_seconds: int) -> None:
    """Sample forever until the owning lifespan task is cancelled."""
    next_retention_at = time.monotonic()
    while True:
        try:
            async with session_factory() as session:
                await sample_once(session)
        except Exception:
            logger.warning("System metric sampling failed", exc_info=True)

        now = time.monotonic()
        if now >= next_retention_at:
            try:
                async with session_factory() as session:
                    await run_retention(session)
            except Exception:
                logger.warning("Log retention cleanup failed", exc_info=True)
            next_retention_at = now + _RETENTION_INTERVAL_SECONDS

        await asyncio.sleep(interval_seconds)


async def list_history(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[list[SystemMetric], int]:
    """Return a newest-first page of sampled metrics."""
    return await system_metric_repository.list_metrics(
        session,
        page=page,
        page_size=page_size,
        start_time=_to_utc_naive(start_time),
        end_time=_to_utc_naive(end_time),
    )


def _to_utc_naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


__all__: tuple[str, ...] = (
    "collect_live_snapshot",
    "collect_psutil_snapshot",
    "list_history",
    "run_retention",
    "run_sampler",
    "sample_once",
)
