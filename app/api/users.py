"""用户管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.redis import get_redis
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.user import UserStatus
from app.schemas.user import (
    UserCreate,
    UserDepartmentUpdate,
    UserPasswordReset,
    UserRead,
    UserRoleUpdate,
    UserStatusUpdate,
    UserUpdate,
)
from app.services import user as user_service

router = APIRouter(prefix="/users", tags=["用户"])


@router.get(
    "",
    response_model=SuccessEnvelope[Page[UserRead]],
    dependencies=[Depends(require_permission("user:read"))],
)
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    department_id: int | None = None,
    role_id: int | None = None,
    user_status: Annotated[UserStatus | None, Query(alias="status")] = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    users, total = await user_service.list_users(
        session,
        context=context,
        page=page,
        page_size=page_size,
        department_id=department_id,
        role_id=role_id,
        user_status=user_status,
        keyword=keyword,
    )
    return success(
        Page[UserRead](
            items=[UserRead.model_validate(user) for user in users],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "",
    response_model=SuccessEnvelope[UserRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("user:create"))],
)
async def create_user(
    payload: UserCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    user = await user_service.create_local_user(session, payload)
    return success(UserRead.model_validate(user), message="created")


@router.get(
    "/{user_id}",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:read"))],
)
async def get_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    user = await user_service.get_user(session, user_id, context=context)
    return success(UserRead.model_validate(user))


@router.patch(
    "/{user_id}",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:update"))],
)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    user = await user_service.update_user(
        session,
        user_id,
        payload,
        context=context,
    )
    return success(UserRead.model_validate(user))


@router.delete(
    "/{user_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("user:delete"))],
)
async def delete_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    await user_service.delete_user(
        session,
        user_id,
        context=context,
        redis=redis,
    )
    return success(message="deleted")


@router.patch(
    "/{user_id}/role",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:assign"))],
)
async def assign_user_role(
    user_id: int,
    payload: UserRoleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    user = await user_service.assign_user_role(
        session,
        user_id,
        payload,
        context=context,
        redis=redis,
    )
    return success(UserRead.model_validate(user))


@router.patch(
    "/{user_id}/department",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:assign"))],
)
async def assign_user_department(
    user_id: int,
    payload: UserDepartmentUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    user = await user_service.assign_user_department(
        session,
        user_id,
        payload,
        context=context,
        redis=redis,
    )
    return success(UserRead.model_validate(user))


@router.patch(
    "/{user_id}/status",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:update"))],
)
async def update_user_status(
    user_id: int,
    payload: UserStatusUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    user = await user_service.update_user_status(
        session,
        user_id,
        payload,
        context=context,
        redis=redis,
    )
    return success(UserRead.model_validate(user))


@router.post(
    "/{user_id}/password/reset",
    response_model=SuccessEnvelope[UserRead],
    dependencies=[Depends(require_permission("user:update"))],
)
async def reset_user_password(
    user_id: int,
    payload: UserPasswordReset,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    user = await user_service.reset_user_password(
        session,
        user_id,
        payload,
        context=context,
        redis=redis,
    )
    return success(UserRead.model_validate(user), message="password reset")


__all__: tuple[str, ...] = (
    "assign_user_department",
    "assign_user_role",
    "create_user",
    "delete_user",
    "get_user",
    "list_users",
    "reset_user_password",
    "router",
    "update_user",
    "update_user_status",
)
