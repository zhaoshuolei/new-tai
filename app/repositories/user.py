"""用户数据访问。"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.user import User, UserStatus


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_username(session: AsyncSession, username: str) -> User | None:
    statement = select(User).where(User.username == username)
    return await session.scalar(statement)


async def add(session: AsyncSession, user: User) -> User:
    session.add(user)
    await session.flush()
    return user


async def list_users(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    department_id: int | None,
    role_id: int | None,
    user_status: UserStatus | None,
    keyword: str | None,
) -> tuple[list[User], int]:
    conditions: list[ColumnElement[bool]] = []
    if department_id is not None:
        conditions.append(User.department_id == department_id)
    if role_id is not None:
        conditions.append(User.role_id == role_id)
    if user_status is not None:
        conditions.append(User.status == user_status)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                User.username.like(pattern),
                User.nickname.like(pattern),
                User.email.like(pattern),
            )
        )

    total_statement = select(func.count()).select_from(User).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(User)
        .where(*conditions)
        .order_by(User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.scalars(statement)
    return list(result.all()), int(total or 0)


async def delete(session: AsyncSession, user: User) -> None:
    await session.delete(user)


__all__: tuple[str, ...] = (
    "add",
    "delete",
    "get_by_id",
    "get_by_username",
    "list_users",
)
