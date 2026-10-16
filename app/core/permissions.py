"""功能权限依赖与角色权限缓存。"""

import json
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import key
from app.models.role import Role

PERMISSION_CACHE_TTL_SECONDS = 600


def _role_permission_key(role_id: int) -> str:
    return key("perm", "role", str(role_id))


def _super_admin_permission_key() -> str:
    return key("perm", "super_admin")


def _serialize_permission_codes(codes: frozenset[str]) -> str:
    return json.dumps(sorted(codes), separators=(",", ":"))


def _deserialize_permission_codes(raw: str | bytes) -> frozenset[str] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError, TypeError, UnicodeDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
        return None
    return frozenset(value)


async def get_role_permission_codes(
    session: AsyncSession,
    redis: Redis,
    role: Role,
) -> frozenset[str]:
    """返回角色权限码；优先读取角色维度缓存。"""
    cache_key = (
        _super_admin_permission_key()
        if role.code == "super_admin" and role.is_system
        else _role_permission_key(role.id)
    )
    cached = await redis.get(cache_key)
    if isinstance(cached, str | bytes):
        codes = _deserialize_permission_codes(cached)
        if codes is not None:
            return codes
        await redis.delete(cache_key)

    from app.services.role import list_role_permission_codes

    codes = await list_role_permission_codes(session, role)
    await redis.set(
        cache_key,
        _serialize_permission_codes(codes),
        ex=PERMISSION_CACHE_TTL_SECONDS,
    )
    return codes


async def invalidate_role_permissions(redis: Redis, role_id: int) -> None:
    """清除指定角色的权限码缓存。"""
    await redis.delete(_role_permission_key(role_id))


async def invalidate_super_admin_permissions(redis: Redis) -> None:
    """清除超级管理员的动态权限码缓存。"""
    await redis.delete(_super_admin_permission_key())


def require_permission(
    code: str,
) -> Callable[..., Awaitable[AuthContext]]:
    """返回校验指定权限码的 FastAPI 依赖。"""
    from app.core.deps import get_current_context

    async def dependency(
        context: Annotated[AuthContext, Depends(get_current_context)],
    ) -> AuthContext:
        if code not in context.permission_codes:
            raise BusinessError(
                ErrorCode.FORBIDDEN,
                "Permission denied",
                http_status=403,
            )
        return context

    return dependency


__all__: tuple[str, ...] = (
    "PERMISSION_CACHE_TTL_SECONDS",
    "get_role_permission_codes",
    "invalidate_role_permissions",
    "invalidate_super_admin_permissions",
    "require_permission",
)
