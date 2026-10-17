import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.router import api_v1_router
from app.core.config import (
    normalize_media_url_path,
    resolve_media_root,
    settings,
)
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware, configure_security
from app.core.redis import redis_client
from app.db.session import engine
from app.services.monitor import run_sampler

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "认证", "description": "认证与会话管理"},
    {"name": "用户", "description": "用户管理"},
    {"name": "角色", "description": "角色管理"},
    {"name": "菜单", "description": "菜单与权限管理"},
    {"name": "部门", "description": "部门管理"},
    {"name": "客户", "description": "客户档案与负责人管理"},
    {"name": "集成", "description": "第三方集成配置"},
    {"name": "系统设置", "description": "网站基础信息与品牌资源"},
    {"name": "健康", "description": "服务健康检查"},
    {"name": "个人资料", "description": "本人资料维护"},
]


def _validate_critical_settings() -> None:
    required_fields = (
        "database_url",
        "redis_url",
        "jwt_secret",
        "config_encryption_key",
    )
    for field_name in required_fields:
        if not getattr(settings, field_name).strip():
            raise RuntimeError(f"Missing required setting: {field_name}")
    try:
        Fernet(settings.config_encryption_key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid config_encryption_key") from exc


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


async def _stop_sampler(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.redis_client = None
    application.state.db_engine = None
    application.state.monitor_sampler_task = None
    sampler_task: asyncio.Task[None] | None = None

    try:
        _validate_critical_settings()
        application.state.redis_client = redis_client
        application.state.db_engine = engine
        await redis_client.ping()
        if settings.monitor_enabled:
            sampler_task = asyncio.create_task(
                run_sampler(settings.monitor_sample_interval_seconds),
                name="system-metric-sampler",
            )
            application.state.monitor_sampler_task = sampler_task
        yield
    except BaseException as primary_error:
        await _stop_sampler(sampler_task)
        application.state.monitor_sampler_task = None
        cleanup_errors = await _cleanup_resources(redis_client, engine)
        application.state.redis_client = None
        application.state.db_engine = None
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Application startup or request failed during cleanup",
                [primary_error, *cleanup_errors],
            ) from primary_error
        raise
    else:
        await _stop_sampler(sampler_task)
        application.state.monitor_sampler_task = None
        cleanup_errors = await _cleanup_resources(redis_client, engine)
        application.state.redis_client = None
        application.state.db_engine = None
        _raise_cleanup_errors(cleanup_errors)


def create_app() -> FastAPI:
    configure_logging(settings.env)
    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    configure_security(application)
    application.add_middleware(RequestContextMiddleware)
    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(api_v1_router)
    media_url_path = normalize_media_url_path(settings.media_url_path)
    media_root = resolve_media_root(settings.media_root)
    media_root.mkdir(parents=True, exist_ok=True)
    application.mount(
        media_url_path,
        StaticFiles(directory=str(media_root), check_dir=False),
        name="media",
    )
    return application


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app", "lifespan")
