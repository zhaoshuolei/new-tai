"""Current-user profile API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.profile import ProfileRead, ProfileUpdate
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


register_audit_route(
    "PATCH",
    "/me/profile",
    action="user_profile:update",
    resource_type="user_profile",
)


__all__: tuple[str, ...] = ("get_my_profile", "router", "update_my_profile")
