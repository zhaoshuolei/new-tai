"""用户数据访问。"""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.user import User, UserStatus


async def get_by_id(
    session: AsyncSession,
    user_id: int,
    *,
    include_deleted: bool = False,
) -> User | None:
    statement = select(User).where(User.id == user_id)
    if not include_deleted:
        statement = statement.where(User.deleted_at.is_(None))
    return await session.scalar(statement)


async def lock_by_id(
    session: AsyncSession,
    user_id: int,
    *,
    include_deleted: bool = False,
) -> User | None:
    statement = select(User).where(User.id == user_id)
    if not include_deleted:
        statement = statement.where(User.deleted_at.is_(None))
    statement = statement.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(statement)


async def get_by_username(session: AsyncSession, username: str) -> User | None:
    statement = select(User).where(
        User.username == username,
        User.deleted_at.is_(None),
    )
    return await session.scalar(statement)


async def get_by_feishu_open_id(
    session: AsyncSession,
    feishu_open_id: str,
) -> User | None:
    statement = select(User).where(
        User.feishu_open_id == feishu_open_id,
        User.deleted_at.is_(None),
    )
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
    scope_filter: ColumnElement[bool],
    department_id: int | None,
    role_id: int | None,
    user_status: UserStatus | None,
    keyword: str | None,
) -> tuple[list[User], int]:
    conditions: list[ColumnElement[bool]] = [
        scope_filter,
        User.deleted_at.is_(None),
    ]
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


async def soft_delete(
    session: AsyncSession,
    user: User,
    *,
    deleted_by: int,
) -> None:
    user.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    user.deleted_by = deleted_by


async def restore(session: AsyncSession, user: User) -> None:
    user.deleted_at = None
    user.deleted_by = None


async def list_deleted_users(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    scope_filter: ColumnElement[bool],
) -> tuple[list[User], int]:
    conditions = [scope_filter, User.deleted_at.is_not(None)]
    total = await session.scalar(
        select(func.count()).select_from(User).where(*conditions)
    )
    statement = (
        select(User)
        .where(*conditions)
        .order_by(User.deleted_at.desc(), User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.scalars(statement)
    return list(result.all()), int(total or 0)


__all__: tuple[str, ...] = (
    "add",
    "get_by_feishu_open_id",
    "get_by_id",
    "get_by_username",
    "list_deleted_users",
    "list_users",
    "lock_by_id",
    "restore",
    "soft_delete",
)
