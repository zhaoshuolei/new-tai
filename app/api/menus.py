"""菜单管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_session
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate
from app.services import menu as menu_service

router = APIRouter(prefix="/menus", tags=["菜单"])


async def require_menu_read() -> None:
    """Task 15 接入菜单读取权限前的占位依赖。"""


async def require_menu_create() -> None:
    """Task 15 接入菜单创建权限前的占位依赖。"""


async def require_menu_update() -> None:
    """Task 15 接入菜单更新权限前的占位依赖。"""


async def require_menu_delete() -> None:
    """Task 15 接入菜单删除权限前的占位依赖。"""


@router.get(
    "/tree",
    dependencies=[Depends(require_menu_read)],
)
async def get_menu_tree(
    session: Annotated[AsyncSession, Depends(get_session)],
    include_disabled: bool = False,
) -> dict[str, Any]:
    tree = await menu_service.get_menu_tree(
        session,
        include_disabled=include_disabled,
    )
    return success(tree)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_menu_create)],
)
async def create_menu(
    payload: MenuCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    menu = await menu_service.create_menu(session, payload)
    return success(
        MenuNode.model_validate(menu),
        message="created",
    )


@router.patch(
    "/{menu_id}",
    dependencies=[Depends(require_menu_update)],
)
async def update_menu(
    menu_id: int,
    payload: MenuUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    menu = await menu_service.update_menu(session, menu_id, payload)
    return success(MenuNode.model_validate(menu))


@router.delete(
    "/{menu_id}",
    dependencies=[Depends(require_menu_delete)],
)
async def delete_menu(
    menu_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await menu_service.delete_menu(session, menu_id)
    return success(message="deleted")


__all__: tuple[str, ...] = (
    "create_menu",
    "delete_menu",
    "get_menu_tree",
    "router",
    "update_menu",
)
