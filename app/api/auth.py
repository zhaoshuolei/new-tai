"""认证接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.feishu import FeishuClient, get_feishu_client
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.core.response import success
from app.db.session import get_session
from app.repositories import user as user_repository
from app.schemas.auth import (
    CurrentUserRead,
    FeishuLoginRequest,
    LoginRead,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenRead,
)
from app.schemas.user import UserRead
from app.services import auth as auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    result = await auth_service.login_local_user(
        session,
        redis,
        username=payload.username,
        password=payload.password,
        client_ip=request.client.host if request.client is not None else "unknown",
        device_id=payload.device_id,
    )
    response = LoginRead(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        token_type=result.tokens.token_type,
        expires_in=result.tokens.expires_in,
        profile=UserRead.model_validate(result.user),
    )
    return success(response)


@router.post("/feishu/login")
async def feishu_login(
    payload: FeishuLoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    feishu_client: Annotated[FeishuClient, Depends(get_feishu_client)],
) -> dict[str, Any]:
    access_token, refresh_token, user = await auth_service.login_with_feishu(
        session,
        redis,
        payload.code,
        client=feishu_client,
    )
    response = LoginRead(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        profile=UserRead.model_validate(user),
    )
    return success(response)


@router.post("/refresh")
async def refresh(
    payload: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    tokens = await auth_service.rotate_refresh_token(
        session,
        redis,
        payload.refresh_token,
    )
    return success(
        TokenRead(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
        )
    )


@router.post("/logout")
async def logout(
    payload: LogoutRequest,
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    await auth_service.logout_refresh_token(redis, payload.refresh_token)
    return success(message="logged out")


@router.post("/logout-all")
async def logout_all(
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    await auth_service.invalidate_refresh_tokens(redis, context.user_id)
    return success(message="logged out from all devices")


@router.get("/me")
async def get_me(
    context: Annotated[AuthContext, Depends(get_current_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    user = await user_repository.get_by_id(session, context.user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )

    profile = CurrentUserRead(
        **UserRead.model_validate(user).model_dump(),
        data_scope=context.data_scope,
        custom_department_ids=sorted(context.custom_department_ids),
        include_children=context.include_children,
        permission_codes=sorted(context.permission_codes),
        is_super_admin=context.is_super_admin,
    )
    return success(profile)


__all__: tuple[str, ...] = (
    "feishu_login",
    "get_me",
    "login",
    "logout",
    "logout_all",
    "refresh",
    "router",
)
