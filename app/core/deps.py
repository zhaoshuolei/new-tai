"""认证请求上下文的 FastAPI 依赖。"""

from typing import Annotated

from fastapi import Depends, Header
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import UserStatus
from app.services.auth import build_auth_context, get_auth_snapshot


async def get_current_context(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """校验 Bearer token 与认证快照并组装请求上下文。"""
    if authorization is None:
        raise BusinessError(
            ErrorCode.UNAUTHENTICATED,
            "Authentication required",
            http_status=401,
        )

    scheme, separator, token = authorization.partition(" ")
    if separator == "" or scheme.lower() != "bearer" or not token:
        raise BusinessError(
            ErrorCode.UNAUTHENTICATED,
            "Authentication required",
            http_status=401,
        )

    claims = decode_access_token(token)
    user_id = int(claims["sub"])
    token_version = int(claims["ver"])
    snapshot = await get_auth_snapshot(session, redis, user_id)

    if snapshot.status == UserStatus.DISABLED:
        raise BusinessError(
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled",
            http_status=401,
        )
    if snapshot.token_version != token_version:
        raise BusinessError(
            ErrorCode.TOKEN_EXPIRED,
            "Token expired",
            http_status=401,
        )

    return await build_auth_context(session, user_id, snapshot)


__all__: tuple[str, ...] = ("get_current_context",)
