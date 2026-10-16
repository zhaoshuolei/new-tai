from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


async def _cleanup_resources(
    redis_client: Redis | None,
    db_engine: AsyncEngine | None,
) -> list[Exception]:
    errors: list[Exception] = []

    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception as exc:
            errors.append(exc)

    if db_engine is not None:
        try:
            await db_engine.dispose()
        except Exception as exc:
            errors.append(exc)

    return errors


def _raise_cleanup_errors(errors: list[Exception]) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise ExceptionGroup("Application resource cleanup failed", errors)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    redis_client: Redis | None = None
    db_engine: AsyncEngine | None = None
    application.state.redis_client = None
    application.state.db_engine = None

    try:
        _validate_critical_settings()
        redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
        application.state.redis_client = redis_client
        assert redis_client is not None
        await redis_client.ping()
        db_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        application.state.db_engine = db_engine
        yield
    except BaseException as primary_error:
        cleanup_errors = await _cleanup_resources(redis_client, db_engine)
        application.state.redis_client = None
        application.state.db_engine = None
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Application startup or request failed during cleanup",
                [primary_error, *cleanup_errors],
            ) from primary_error
        raise
    else:
        cleanup_errors = await _cleanup_resources(redis_client, db_engine)
        application.state.redis_client = None
        application.state.db_engine = None
        _raise_cleanup_errors(cleanup_errors)


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    return application


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app", "lifespan")
