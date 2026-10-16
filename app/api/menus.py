"""菜单管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import success
from app.db.session import get_session
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate
from app.services import menu as menu_service

router = APIRouter(prefix="/menus", tags=["菜单"])
me_router = APIRouter(prefix="/me", tags=["当前用户"])


@router.get(
    "/tree",
    dependencies=[Depends(require_permission("menu:read"))],
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
    dependencies=[Depends(require_permission("menu:create"))],
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
    dependencies=[Depends(require_permission("menu:update"))],
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
    dependencies=[Depends(require_permission("menu:delete"))],
)
async def delete_menu(
    menu_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await menu_service.delete_menu(session, menu_id)
    return success(message="deleted")


@me_router.get("/permissions")
async def get_my_permissions(
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    return success(sorted(context.permission_codes))


@me_router.get("/menus")
async def get_my_menus(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    tree = await menu_service.get_visible_menu_tree(
        session,
        role_id=context.role_id,
        is_super_admin=context.is_super_admin,
    )
    return success(tree)


__all__: tuple[str, ...] = (
    "create_menu",
    "delete_menu",
    "get_menu_tree",
    "get_my_menus",
    "get_my_permissions",
    "me_router",
    "router",
    "update_menu",
)
