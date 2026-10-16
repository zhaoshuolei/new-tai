"""角色管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
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


async def require_role_read() -> None:
    """Task 15 接入角色读取权限前的占位依赖。"""


async def require_role_create() -> None:
    """Task 15 接入角色创建权限前的占位依赖。"""


async def require_role_update() -> None:
    """Task 15 接入角色更新权限前的占位依赖。"""


async def require_role_delete() -> None:
    """Task 15 接入角色删除权限前的占位依赖。"""


async def require_role_assign() -> None:
    """Task 15 接入角色授权权限前的占位依赖。"""


@router.get(
    "",
    dependencies=[Depends(require_role_read)],
)
async def list_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    roles = await role_service.list_roles(session)
    return success([RoleRead.model_validate(role) for role in roles])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role_create)],
)
async def create_role(
    payload: RoleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.create_role(session, payload)
    return success(RoleRead.model_validate(role), message="created")


@router.get(
    "/{role_id}",
    dependencies=[Depends(require_role_read)],
)
async def get_role(
    role_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.get_role(session, role_id)
    return success(RoleRead.model_validate(role))


@router.patch(
    "/{role_id}",
    dependencies=[Depends(require_role_update)],
)
async def update_role(
    role_id: int,
    payload: RoleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.update_role(session, role_id, payload)
    return success(RoleRead.model_validate(role))


@router.delete(
    "/{role_id}",
    dependencies=[Depends(require_role_delete)],
)
async def delete_role(
    role_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await role_service.delete_role(session, role_id)
    return success(message="deleted")


@router.put(
    "/{role_id}/menus",
    dependencies=[Depends(require_role_assign)],
)
async def assign_role_menus(
    role_id: int,
    payload: RoleMenuAssign,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.assign_role_menus(session, role_id, payload.menu_ids)
    return success(RoleRead.model_validate(role))


@router.put(
    "/{role_id}/data-scope",
    dependencies=[Depends(require_role_assign)],
)
async def set_role_data_scope(
    role_id: int,
    payload: RoleDataScopeUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    role = await role_service.set_role_data_scope(session, role_id, payload)
    return success(RoleRead.model_validate(role))


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
