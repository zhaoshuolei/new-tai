from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "认证", "description": "认证与会话管理"},
    {"name": "用户", "description": "用户管理"},
    {"name": "角色", "description": "角色管理"},
    {"name": "菜单", "description": "菜单与权限管理"},
    {"name": "部门", "description": "部门管理"},
    {"name": "健康", "description": "服务健康检查"},
]


def _validate_critical_settings() -> None:
    for field_name in ("database_url", "redis_url", "jwt_secret"):
        if not getattr(settings, field_name).strip():
            raise RuntimeError(f"Missing required setting: {field_name}")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    _validate_critical_settings()
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    db_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    application.state.redis_client = redis_client
    application.state.db_engine = db_engine
    try:
        yield
    finally:
        await redis_client.aclose()
        await db_engine.dispose()
        application.state.redis_client = None
        application.state.db_engine = None


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    return application


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app", "lifespan")
