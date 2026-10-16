"""部门管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_permission
from app.core.response import success
from app.db.session import get_session
from app.schemas.department import (
    DepartmentCreate,
    DepartmentMove,
    DepartmentNode,
    DepartmentUpdate,
)
from app.services import department as department_service

router = APIRouter(prefix="/departments", tags=["部门"])


@router.get(
    "/tree",
    dependencies=[Depends(require_permission("dept:read"))],
)
async def get_department_tree(
    session: Annotated[AsyncSession, Depends(get_session)],
    include_disabled: bool = False,
) -> dict[str, Any]:
    tree = await department_service.get_tree(
        session,
        include_disabled=include_disabled,
    )
    return success(tree)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("dept:create"))],
)
async def create_department(
    payload: DepartmentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    department = await department_service.create_department(session, payload)
    return success(
        DepartmentNode.model_validate(department),
        message="created",
    )


@router.patch(
    "/{department_id}",
    dependencies=[Depends(require_permission("dept:update"))],
)
async def update_department(
    department_id: int,
    payload: DepartmentUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    department = await department_service.update_department(
        session,
        department_id,
        payload,
    )
    return success(DepartmentNode.model_validate(department))


@router.patch(
    "/{department_id}/move",
    dependencies=[Depends(require_permission("dept:update"))],
)
async def move_department(
    department_id: int,
    payload: DepartmentMove,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    department = await department_service.move_department(
        session,
        department_id,
        payload.new_parent_id,
    )
    return success(DepartmentNode.model_validate(department))


@router.delete(
    "/{department_id}",
    dependencies=[Depends(require_permission("dept:delete"))],
)
async def delete_department(
    department_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await department_service.delete_department(session, department_id)
    return success(message="deleted")


__all__: tuple[str, ...] = (
    "create_department",
    "delete_department",
    "get_department_tree",
    "move_department",
    "router",
    "update_department",
)
