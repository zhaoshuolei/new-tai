"""异常日志数据访问。"""

from datetime import datetime

from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.datetime_utils import to_utc_naive
from app.models.exception_log import ExceptionLevel, ExceptionLog

# Cap the number of fingerprint groups a single summary request can return so an
# unbounded table (no time range, retention disabled) cannot materialize an
# unbounded result set.
MAX_SUMMARY_GROUPS = 200


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
    normalized_start_time = to_utc_naive(start_time)
    normalized_end_time = to_utc_naive(end_time)
    if normalized_start_time is not None:
        conditions.append(ExceptionLog.occurred_at >= normalized_start_time)
    if normalized_end_time is not None:
        conditions.append(ExceptionLog.occurred_at <= normalized_end_time)

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
    normalized_start_time = to_utc_naive(start_time)
    normalized_end_time = to_utc_naive(end_time)
    if normalized_start_time is not None:
        conditions.append(ExceptionLog.occurred_at >= normalized_start_time)
    if normalized_end_time is not None:
        conditions.append(ExceptionLog.occurred_at <= normalized_end_time)

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
        .limit(MAX_SUMMARY_GROUPS)
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
    exception_id: int,
    *,
    user_id: int,
    at: datetime,
) -> bool:
    """Atomically acknowledge an unacknowledged row, returning whether it matched.

    The guarded ``WHERE acknowledged = false`` clause makes concurrent acks
    safe: only the first one updates the handler and timestamp, later acks match
    zero rows and leave the original values intact.
    """
    statement = (
        update(ExceptionLog)
        .where(
            ExceptionLog.id == exception_id,
            ExceptionLog.acknowledged.is_(False),
        )
        .values(
            acknowledged=True,
            acknowledged_by=user_id,
            acknowledged_at=at,
        )
    )
    result = await session.execute(statement)
    return bool(getattr(result, "rowcount", 0) or 0)


__all__: tuple[str, ...] = (
    "MAX_SUMMARY_GROUPS",
    "ack",
    "add",
    "get_by_id",
    "list_exceptions",
    "summary_by_fingerprint",
)
