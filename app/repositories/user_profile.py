"""用户档案数据访问。"""

from typing import Any, NamedTuple, cast

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile


class DirectoryRow(NamedTuple):
    id: int
    nickname: str | None
    username: str | None
    avatar: str | None
    signature: str | None
    hidden_public_fields: list[str] | None
    department_name: str | None


class DirectoryProfileRow(NamedTuple):
    id: int
    nickname: str | None
    username: str | None
    avatar: str | None
    signature: str | None
    bio: str | None
    work_phone: str | None
    extension: str | None
    work_email: str | None
    office_location: str | None
    seat: str | None
    expertise_tags: list[Any] | None
    personal_links: list[Any] | None
    hidden_public_fields: list[str] | None
    department_name: str | None
    status: UserStatus


async def get_by_user_id(
    session: AsyncSession,
    user_id: int,
) -> UserProfile | None:
    statement = select(UserProfile).where(UserProfile.user_id == user_id)
    return await session.scalar(statement)


async def lock_by_user_id(
    session: AsyncSession,
    user_id: int,
) -> UserProfile | None:
    statement = (
        select(UserProfile)
        .where(UserProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def add(session: AsyncSession, profile: UserProfile) -> UserProfile:
    """添加档案；仅 flush，事务边界由服务层控制。"""
    session.add(profile)
    await session.flush()
    return profile


async def list_directory_entries(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    keyword: str | None,
    department_id: int | None,
) -> tuple[list[DirectoryRow], int]:
    conditions = [
        User.deleted_at.is_(None),
        User.status == UserStatus.ACTIVE,
    ]
    if department_id is not None:
        conditions.append(User.department_id == department_id)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                User.nickname.like(pattern),
                User.username.like(pattern),
            )
        )

    total_statement = (
        select(func.count())
        .select_from(User)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(Department, Department.id == User.department_id)
        .where(*conditions)
    )
    total = await session.scalar(total_statement)

    statement = (
        select(
            User.id,
            User.nickname,
            User.username,
            User.avatar,
            UserProfile.signature,
            UserProfile.hidden_public_fields,
            Department.name,
        )
        .select_from(User)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(Department, Department.id == User.department_id)
        .where(*conditions)
        .order_by(
            case((Department.sort_order.is_(None), 1), else_=0),
            Department.sort_order,
            func.coalesce(User.nickname, User.username),
            User.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(statement)
    rows = [DirectoryRow(*row) for row in result.all()]
    return rows, int(total or 0)


async def get_directory_profile_row(
    session: AsyncSession,
    user_id: int,
) -> DirectoryProfileRow | None:
    statement = (
        select(
            User.id,
            User.nickname,
            User.username,
            User.avatar,
            UserProfile.signature,
            UserProfile.bio,
            UserProfile.work_phone,
            UserProfile.extension,
            UserProfile.work_email,
            UserProfile.office_location,
            UserProfile.seat,
            UserProfile.expertise_tags,
            UserProfile.personal_links,
            UserProfile.hidden_public_fields,
            Department.name,
            User.status,
        )
        .select_from(User)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(Department, Department.id == User.department_id)
        .where(
            User.id == user_id,
            User.deleted_at.is_(None),
            User.status == UserStatus.ACTIVE,
        )
    )
    result = await session.execute(statement)
    rows = result.all()
    if len(rows) == 0:
        return None
    return DirectoryProfileRow(*cast(Any, rows[0]))


__all__: tuple[str, ...] = (
    "DirectoryProfileRow",
    "DirectoryRow",
    "add",
    "get_by_user_id",
    "get_directory_profile_row",
    "list_directory_entries",
    "lock_by_user_id",
)
