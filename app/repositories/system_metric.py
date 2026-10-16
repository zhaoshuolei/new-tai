"""系统监控采样数据访问。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.system_metric import SystemMetric


async def add(session: AsyncSession, row: SystemMetric) -> SystemMetric:
    session.add(row)
    await session.flush()
    return row


async def list_metrics(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[list[SystemMetric], int]:
    conditions: list[ColumnElement[bool]] = []
    if start_time is not None:
        conditions.append(SystemMetric.sampled_at >= start_time)
    if end_time is not None:
        conditions.append(SystemMetric.sampled_at <= end_time)

    total_statement = select(func.count()).select_from(SystemMetric).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(SystemMetric)
        .where(*conditions)
        .order_by(SystemMetric.sampled_at.desc(), SystemMetric.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result_rows = await session.scalars(statement)
    return list(result_rows.all()), int(total or 0)


__all__: tuple[str, ...] = ("add", "list_metrics")
