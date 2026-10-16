from redis.asyncio import Redis

from app.core.config import settings

redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> Redis:
    return redis_client


def key(*parts: str) -> str:
    return ":".join([settings.app_name, settings.env, *parts])


__all__: tuple[str, ...] = ("Redis", "get_redis", "key", "redis_client")
