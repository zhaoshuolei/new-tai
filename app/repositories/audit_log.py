"""操作日志数据访问。"""

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.datetime_utils import to_utc_naive
from app.models.audit_log import AuditLog, AuditResult


async def add(session: AsyncSession, row: AuditLog) -> AuditLog:
    session.add(row)
    await session.flush()
    return row


async def list_audit_logs(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    user_id: int | None,
    action: str | None,
    resource_type: str | None,
    resource_id: int | None,
    result: AuditResult | None,
    start_time: datetime | None,
    end_time: datetime | None,
    keyword: str | None,
) -> tuple[list[AuditLog], int]:
    conditions: list[ColumnElement[bool]] = []
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if action is not None:
        conditions.append(AuditLog.action == action)
    if resource_type is not None:
        conditions.append(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        conditions.append(AuditLog.resource_id == resource_id)
    if result is not None:
        conditions.append(AuditLog.result == result)
    normalized_start_time = to_utc_naive(start_time)
    normalized_end_time = to_utc_naive(end_time)
    if normalized_start_time is not None:
        conditions.append(AuditLog.created_at >= normalized_start_time)
    if normalized_end_time is not None:
        conditions.append(AuditLog.created_at <= normalized_end_time)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                AuditLog.actor_username.like(pattern),
                AuditLog.actor_nickname.like(pattern),
            )
        )

    total_statement = select(func.count()).select_from(AuditLog).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result_rows = await session.scalars(statement)
    return list(result_rows.all()), int(total or 0)


__all__: tuple[str, ...] = ("add", "list_audit_logs")
