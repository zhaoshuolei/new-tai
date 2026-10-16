"""可复用的依赖探活函数。"""

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def check_database(session: AsyncSession) -> bool:
    """Return whether the database accepts a trivial query."""
    try:
        await session.execute(select(1))
    except Exception:
        return False
    return True


async def check_redis(client: Redis) -> bool:
    """Return whether Redis responds to a ping."""
    try:
        return bool(await client.ping())
    except Exception:
        return False


__all__: tuple[str, ...] = ("check_database", "check_redis")
