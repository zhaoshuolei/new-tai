"""可复用的依赖探活函数。"""

import asyncio

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

HEALTH_PROBE_TIMEOUT_SECONDS = 2.0


async def _discard_database_session(session: AsyncSession) -> None:
    try:
        await session.invalidate()
    except Exception:
        return


async def _discard_redis_connection(client: Redis) -> None:
    try:
        await client.connection_pool.disconnect(inuse_connections=True)
    except Exception:
        return


async def check_database(session: AsyncSession) -> bool:
    """Return whether the database accepts a trivial query."""
    try:
        await asyncio.wait_for(
            session.execute(select(1)),
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        await _discard_database_session(session)
        return False
    return True


async def check_redis(client: Redis) -> bool:
    """Return whether Redis responds to a ping."""
    try:
        return bool(
            await asyncio.wait_for(
                client.ping(),
                timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
            )
        )
    except Exception:
        await _discard_redis_connection(client)
        return False


__all__: tuple[str, ...] = (
    "HEALTH_PROBE_TIMEOUT_SECONDS",
    "check_database",
    "check_redis",
)
