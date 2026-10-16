"""异常日志数据访问。"""

from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.exception_log import ExceptionLevel, ExceptionLog


async def add(session: AsyncSession, row: ExceptionLog) -> ExceptionLog:
    session.add(row)
    await session.flush()
    return row


async def list_exceptions(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    exception_type: str | None,
    level: ExceptionLevel | None,
    acknowledged: bool | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[list[ExceptionLog], int]:
    conditions: list[ColumnElement[bool]] = []
    if exception_type is not None:
        conditions.append(ExceptionLog.exception_type == exception_type)
    if level is not None:
        conditions.append(ExceptionLog.level == level)
    if acknowledged is not None:
        conditions.append(ExceptionLog.acknowledged == acknowledged)
    if start_time is not None:
        conditions.append(ExceptionLog.occurred_at >= start_time)
    if end_time is not None:
        conditions.append(ExceptionLog.occurred_at <= end_time)

    total_statement = select(func.count()).select_from(ExceptionLog).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(ExceptionLog)
        .where(*conditions)
        .order_by(ExceptionLog.occurred_at.desc(), ExceptionLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result_rows = await session.scalars(statement)
    return list(result_rows.all()), int(total or 0)


async def summary_by_fingerprint(
    session: AsyncSession,
    *,
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[Row[str, str, ExceptionLevel, int, datetime, bool]]:
    conditions: list[ColumnElement[bool]] = []
    if start_time is not None:
        conditions.append(ExceptionLog.occurred_at >= start_time)
    if end_time is not None:
        conditions.append(ExceptionLog.occurred_at <= end_time)

    statement = (
        select(
            ExceptionLog.fingerprint.label("fingerprint"),
            func.min(ExceptionLog.exception_type).label("exception_type"),
            # level is stable per fingerprint in practice; agg keeps one row per group.
            func.min(ExceptionLog.level).label("level"),
            func.count().label("count"),
            func.max(ExceptionLog.occurred_at).label("last_occurred_at"),
            # min() over a boolean column is true only when every row is true.
            func.min(ExceptionLog.acknowledged).label("acknowledged"),
        )
        .where(*conditions)
        .group_by(ExceptionLog.fingerprint)
        .order_by(func.max(ExceptionLog.occurred_at).desc())
    )
    result_rows = await session.execute(statement)
    return list(result_rows.all())


async def get_by_id(
    session: AsyncSession,
    exception_id: int,
) -> ExceptionLog | None:
    return await session.get(ExceptionLog, exception_id)


async def ack(
    session: AsyncSession,
    row: ExceptionLog,
    *,
    user_id: int,
    at: datetime,
) -> ExceptionLog:
    row.acknowledged = True
    row.acknowledged_by = user_id
    row.acknowledged_at = at
    await session.flush()
    return row


__all__: tuple[str, ...] = (
    "ack",
    "add",
    "get_by_id",
    "list_exceptions",
    "summary_by_fingerprint",
)
