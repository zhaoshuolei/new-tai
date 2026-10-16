from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from app.core.config import settings

redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> Redis:
    return redis_client


def key(*parts: str) -> str:
    return ":".join([settings.app_name, settings.env, *parts])


@asynccontextmanager
async def redis_scope(redis: Redis | None) -> AsyncIterator[Redis]:
    """复用请求级客户端，未提供时为独立调用创建短生命周期客户端。"""
    if redis is not None:
        yield redis
        return

    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


__all__: tuple[str, ...] = (
    "Redis",
    "get_redis",
    "key",
    "redis_client",
    "redis_scope",
)
