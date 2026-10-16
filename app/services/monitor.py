"""系统监控采样领域业务逻辑。"""

import asyncio
from datetime import UTC, datetime
from typing import TypedDict

import psutil
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.db.session import session_factory
from app.models.system_metric import SystemMetric
from app.repositories import system_metric as system_metric_repository
from app.services.health import check_database, check_redis

_BYTES_PER_MB = 1024 * 1024


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


async def run_sampler(interval_seconds: int) -> None:
    """Sample forever until the owning lifespan task is cancelled."""
    try:
        while True:
            async with session_factory() as session:
                await sample_once(session)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise


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
    "run_sampler",
    "sample_once",
)
