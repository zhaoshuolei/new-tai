"""Current-user profile API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.rate_limit import (
    AVATAR_UPLOAD_MAX_REQUESTS,
    AVATAR_UPLOAD_WINDOW_SECONDS,
    avatar_upload_rate_limit_key,
    enforce_rate_limit,
)
from app.core.redis import get_redis
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.profile import (
    AvatarUploadRead,
    PrivacyUpdate,
    ProfileRead,
    ProfileUpdate,
)
from app.services import user_avatar as user_avatar_service
from app.services import user_profile as user_profile_service

router = APIRouter(prefix="/me/profile", tags=["个人资料"])


@router.get(
    "",
    response_model=SuccessEnvelope[ProfileRead],
)
async def get_my_profile(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    profile = await user_profile_service.get_my_profile(
        session,
        user_id=context.user_id,
    )
    return success(profile)


@router.patch(
    "",
    response_model=SuccessEnvelope[ProfileRead],
)
async def update_my_profile(
    payload: ProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    # Keep this guard ahead of the audited service. @audited records every
    # return, including an early one, so an empty PATCH would create a success row.
    if not payload.model_dump(exclude_unset=True):
        profile = await user_profile_service.get_my_profile(
            session,
            user_id=context.user_id,
        )
        return success(profile)

    profile = await user_profile_service.update_my_profile(
        session,
        payload,
        user_id=context.user_id,
        context=context,
    )
    return success(profile, message="updated")


@router.put(
    "/privacy",
    response_model=SuccessEnvelope[ProfileRead],
)
async def update_my_privacy(
    payload: PrivacyUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    profile = await user_profile_service.update_my_privacy(
        session,
        payload,
        user_id=context.user_id,
        context=context,
    )
    return success(profile, message="updated")


@router.post(
    "/avatar",
    response_model=SuccessEnvelope[AvatarUploadRead],
)
async def upload_my_avatar(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    await enforce_rate_limit(
        redis,
        (avatar_upload_rate_limit_key(context.user_id),),
        window_seconds=AVATAR_UPLOAD_WINDOW_SECONDS,
        max_requests=AVATAR_UPLOAD_MAX_REQUESTS,
        message="Too many avatar uploads; wait before retrying",
    )
    url = await user_avatar_service.upload_avatar(
        session,
        user_id=context.user_id,
        content_type=request.headers.get("content-type"),
        chunks=request.stream(),
        context=context,
    )
    return success({"url": url})


@router.delete(
    "/avatar",
    response_model=SuccessEnvelope[AvatarUploadRead],
)
async def delete_my_avatar(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await user_avatar_service.delete_avatar(
        session,
        user_id=context.user_id,
        context=context,
    )
    return success({"url": None})


register_audit_route(
    "PATCH",
    "/me/profile",
    action="user_profile:update",
    resource_type="user_profile",
)
register_audit_route(
    "PUT",
    "/me/profile/privacy",
    action="user_profile:privacy",
    resource_type="user_profile",
)
register_audit_route(
    "POST",
    "/me/profile/avatar",
    action="user_profile:avatar_upload",
    resource_type="user_profile",
    resource_id_param="user_id",
)
register_audit_route(
    "DELETE",
    "/me/profile/avatar",
    action="user_profile:avatar_delete",
    resource_type="user_profile",
    resource_id_param="user_id",
)


__all__: tuple[str, ...] = (
    "delete_my_avatar",
    "get_my_profile",
    "router",
    "update_my_privacy",
    "update_my_profile",
    "upload_my_avatar",
)
