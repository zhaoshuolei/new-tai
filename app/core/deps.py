"""认证请求上下文的 FastAPI 依赖。"""

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import UserStatus
from app.repositories import user as user_repository
from app.services.auth import build_auth_context


async def get_current_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """校验 Bearer token 并从数据库权威用户行组装请求上下文。"""
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
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )

    if user.status == UserStatus.DISABLED:
        raise BusinessError(
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled",
            http_status=401,
        )
    if user.token_version != token_version:
        raise BusinessError(
            ErrorCode.TOKEN_EXPIRED,
            "Token expired",
            http_status=401,
        )

    context = await build_auth_context(session, user)
    request.state.user_id = context.user_id
    request.state.actor = ActorSnapshot(
        user_id=context.user_id,
        username=context.username,
        nickname=context.nickname,
    )
    return context


__all__: tuple[str, ...] = ("get_current_context",)
