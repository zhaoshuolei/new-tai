"""用户档案数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_profile import UserProfile


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


__all__: tuple[str, ...] = ("add", "get_by_user_id", "lock_by_user_id")
