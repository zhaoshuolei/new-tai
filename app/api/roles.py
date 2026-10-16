"""角色管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.role import (
    RoleCreate,
    RoleDataScopeUpdate,
    RoleMenuAssign,
    RoleRead,
    RoleUpdate,
)
from app.services import role as role_service

router = APIRouter(prefix="/roles", tags=["角色"])


@router.get(
    "",
    response_model=SuccessEnvelope[list[RoleRead]],
    dependencies=[Depends(require_permission("role:read"))],
)
async def list_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    roles = await role_service.list_roles(session)
    return success([RoleRead.model_validate(role) for role in roles])


@router.post(
    "",
    response_model=SuccessEnvelope[RoleRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("role:create"))],
)
async def create_role(
    payload: RoleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    role = await role_service.create_role(session, payload, context=context)
    return success(RoleRead.model_validate(role), message="created")


@router.get(
    "/{role_id}",
    response_model=SuccessEnvelope[RoleRead],
    dependencies=[Depends(require_permission("role:read"))],
)
async def get_role(
    role_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.get_role(session, role_id)
    return success(RoleRead.model_validate(role))


@router.patch(
    "/{role_id}",
    response_model=SuccessEnvelope[RoleRead],
    dependencies=[Depends(require_permission("role:update"))],
)
async def update_role(
    role_id: int,
    payload: RoleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    role = await role_service.update_role(
        session=session,
        role_id=role_id,
        payload=payload,
        context=context,
    )
    return success(RoleRead.model_validate(role))


@router.delete(
    "/{role_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("role:delete"))],
)
async def delete_role(
    role_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await role_service.delete_role(
        session,
        role_id,
        context=context,
    )
    return success(message="deleted")


@router.put(
    "/{role_id}/menus",
    response_model=SuccessEnvelope[RoleRead],
    dependencies=[Depends(require_permission("role:assign"))],
)
async def assign_role_menus(
    role_id: int,
    payload: RoleMenuAssign,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    role = await role_service.assign_role_menus(
        session,
        role_id,
        payload.menu_ids,
        context=context,
    )
    return success(RoleRead.model_validate(role))


@router.put(
    "/{role_id}/data-scope",
    response_model=SuccessEnvelope[RoleRead],
    dependencies=[Depends(require_permission("role:assign"))],
)
async def set_role_data_scope(
    role_id: int,
    payload: RoleDataScopeUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    role = await role_service.set_role_data_scope(
        session,
        role_id,
        payload,
        context=context,
    )
    return success(RoleRead.model_validate(role))


register_audit_route(
    "POST",
    "/roles",
    action="role:create",
    resource_type="role",
)
register_audit_route(
    "PATCH",
    "/roles/{role_id}",
    action="role:update",
    resource_type="role",
)
register_audit_route(
    "DELETE",
    "/roles/{role_id}",
    action="role:delete",
    resource_type="role",
)
register_audit_route(
    "PUT",
    "/roles/{role_id}/menus",
    action="role:assign",
    resource_type="role",
)
register_audit_route(
    "PUT",
    "/roles/{role_id}/data-scope",
    action="role:assign",
    resource_type="role",
)


__all__: tuple[str, ...] = (
    "assign_role_menus",
    "create_role",
    "delete_role",
    "get_role",
    "list_roles",
    "router",
    "set_role_data_scope",
    "update_role",
)
