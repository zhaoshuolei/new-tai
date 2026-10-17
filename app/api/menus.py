"""菜单管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate
from app.services import menu as menu_service

router = APIRouter(prefix="/menus", tags=["菜单"])
me_router = APIRouter(prefix="/me", tags=["当前用户"])


@router.get(
    "/tree",
    response_model=SuccessEnvelope[list[MenuNode]],
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


@router.get(
    "/recycle-bin",
    response_model=SuccessEnvelope[list[MenuNode]],
    dependencies=[Depends(require_permission("menu:delete"))],
)
async def list_deleted_menus(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    menus = await menu_service.list_deleted_menus(session)
    return success([MenuNode.model_validate(menu) for menu in menus])


@router.post(
    "",
    response_model=SuccessEnvelope[MenuNode],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("menu:create"))],
)
async def create_menu(
    payload: MenuCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    menu = await menu_service.create_menu(session, payload, context=context)
    return success(
        MenuNode.model_validate(menu),
        message="created",
    )


@router.patch(
    "/{menu_id}",
    response_model=SuccessEnvelope[MenuNode],
    dependencies=[Depends(require_permission("menu:update"))],
)
async def update_menu(
    menu_id: int,
    payload: MenuUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    menu = await menu_service.update_menu(
        session,
        menu_id,
        payload,
        context=context,
    )
    return success(MenuNode.model_validate(menu))


@router.delete(
    "/{menu_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("menu:delete"))],
)
async def delete_menu(
    menu_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await menu_service.delete_menu(session, menu_id, context=context)
    return success(message="deleted")


@router.post(
    "/{menu_id}/restore",
    response_model=SuccessEnvelope[MenuNode],
    dependencies=[Depends(require_permission("menu:delete"))],
)
async def restore_menu(
    menu_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    menu = await menu_service.restore_menu(session, menu_id, context=context)
    return success(MenuNode.model_validate(menu))


@me_router.get(
    "/permissions",
    response_model=SuccessEnvelope[list[str]],
)
async def get_my_permissions(
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    return success(sorted(context.permission_codes))


@me_router.get(
    "/menus",
    response_model=SuccessEnvelope[list[MenuNode]],
)
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


register_audit_route(
    "POST",
    "/menus",
    action="menu:create",
    resource_type="menu",
)
register_audit_route(
    "PATCH",
    "/menus/{menu_id}",
    action="menu:update",
    resource_type="menu",
)
register_audit_route(
    "DELETE",
    "/menus/{menu_id}",
    action="menu:delete",
    resource_type="menu",
)
register_audit_route(
    "POST",
    "/menus/{menu_id}/restore",
    action="menu:restore",
    resource_type="menu",
)


__all__: tuple[str, ...] = (
    "create_menu",
    "delete_menu",
    "get_menu_tree",
    "get_my_menus",
    "get_my_permissions",
    "list_deleted_menus",
    "me_router",
    "restore_menu",
    "router",
    "update_menu",
)
