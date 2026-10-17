"""Current-user profile API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.profile import ProfileRead
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


__all__: tuple[str, ...] = ("get_my_profile", "router")
