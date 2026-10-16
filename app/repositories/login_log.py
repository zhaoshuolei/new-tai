"""登录日志数据访问。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.login_log import LoginLog, LoginResultEnum, LoginType


async def add(session: AsyncSession, row: LoginLog) -> LoginLog:
    session.add(row)
    await session.flush()
    return row


async def list_login_logs(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    user_id: int | None,
    username: str | None,
    login_type: LoginType | None,
    result: LoginResultEnum | None,
    ip: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[list[LoginLog], int]:
    conditions: list[ColumnElement[bool]] = []
    if user_id is not None:
        conditions.append(LoginLog.user_id == user_id)
    if username is not None:
        conditions.append(LoginLog.username == username)
    if login_type is not None:
        conditions.append(LoginLog.login_type == login_type)
    if result is not None:
        conditions.append(LoginLog.result == result)
    if ip is not None:
        conditions.append(LoginLog.ip == ip)
    if start_time is not None:
        conditions.append(LoginLog.created_at >= start_time)
    if end_time is not None:
        conditions.append(LoginLog.created_at <= end_time)

    total_statement = select(func.count()).select_from(LoginLog).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(LoginLog)
        .where(*conditions)
        .order_by(LoginLog.created_at.desc(), LoginLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result_rows = await session.scalars(statement)
    return list(result_rows.all()), int(total or 0)


__all__: tuple[str, ...] = ("add", "list_login_logs")
