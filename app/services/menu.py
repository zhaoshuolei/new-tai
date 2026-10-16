"""菜单树业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.menu import Menu, MenuStatus, MenuType
from app.repositories import menu as menu_repository
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate

_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"type", "name", "sort_order", "visible", "status"}
)


async def _require_menu(session: AsyncSession, menu_id: int) -> Menu:
    menu = await menu_repository.get_by_id(session, menu_id)
    if menu is None:
        raise BusinessError(
            ErrorCode.MENU_NOT_FOUND,
            "Menu not found",
            http_status=404,
        )
    return menu


async def _require_parent(
    session: AsyncSession,
    parent_id: int | None,
) -> Menu | None:
    if parent_id is None:
        return None
    return await _require_menu(session, parent_id)


def _is_valid_parent_type(
    menu_type: MenuType,
    parent_type: MenuType | None,
) -> bool:
    if parent_type is None:
        return menu_type in {MenuType.DIR, MenuType.MENU}
    if parent_type == MenuType.DIR:
        return menu_type in {MenuType.DIR, MenuType.MENU}
    if parent_type == MenuType.MENU:
        return menu_type == MenuType.BUTTON
    return False


def _validate_parent(
    menu_type: MenuType,
    parent: Menu | None,
) -> None:
    if not _is_valid_parent_type(menu_type, parent.type if parent else None):
        raise BusinessError(
            ErrorCode.INVALID_MENU_HIERARCHY,
            "Invalid menu hierarchy",
        )


async def _validate_children(
    session: AsyncSession,
    menu: Menu,
    menu_type: MenuType,
) -> None:
    for child in await menu_repository.list_children(session, menu.id):
        if not _is_valid_parent_type(child.type, menu_type):
            raise BusinessError(
                ErrorCode.INVALID_MENU_HIERARCHY,
                "Invalid menu hierarchy",
            )


async def _ensure_parent_is_not_descendant(
    session: AsyncSession,
    menu_id: int,
    parent_id: int | None,
) -> None:
    ancestor_id = parent_id
    visited: set[int] = set()
    while ancestor_id is not None:
        if ancestor_id == menu_id or ancestor_id in visited:
            raise BusinessError(
                ErrorCode.INVALID_MENU_HIERARCHY,
                "Invalid menu hierarchy",
            )
        visited.add(ancestor_id)
        ancestor = await menu_repository.get_by_id(session, ancestor_id)
        if ancestor is None:
            return
        ancestor_id = ancestor.parent_id


async def _ensure_permission_code_available(
    session: AsyncSession,
    permission_code: str | None,
    *,
    exclude_id: int | None = None,
) -> None:
    if permission_code is None:
        return
    existing = await menu_repository.get_by_permission_code(
        session,
        permission_code,
    )
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(
            ErrorCode.PERMISSION_CODE_EXISTS,
            "Permission code already exists",
            http_status=409,
        )


def _is_effectively_enabled(menu: Menu, menus_by_id: dict[int, Menu]) -> bool:
    current: Menu | None = menu
    visited: set[int] = set()
    while current is not None:
        if current.id in visited or current.status != MenuStatus.ACTIVE:
            return False
        visited.add(current.id)
        if current.parent_id is None:
            return True
        current = menus_by_id.get(current.parent_id)
    return False


async def create_menu(session: AsyncSession, payload: MenuCreate) -> Menu:
    parent = await _require_parent(session, payload.parent_id)
    _validate_parent(payload.type, parent)
    await _ensure_permission_code_available(session, payload.permission_code)

    menu = Menu(
        parent_id=payload.parent_id,
        type=payload.type,
        name=payload.name,
        path=payload.path,
        component=payload.component,
        icon=payload.icon,
        permission_code=payload.permission_code,
        sort_order=payload.sort_order,
        visible=payload.visible,
        status=MenuStatus.ACTIVE,
    )
    await menu_repository.add(session, menu)
    await session.commit()
    await session.refresh(menu)
    return menu


async def get_menu_tree(
    session: AsyncSession,
    include_disabled: bool = False,
) -> list[MenuNode]:
    all_menus = await menu_repository.list_all(session, include_disabled=True)
    menus_by_id = {menu.id: menu for menu in all_menus}
    visible_menus = [
        menu
        for menu in all_menus
        if include_disabled or _is_effectively_enabled(menu, menus_by_id)
    ]
    nodes = {menu.id: MenuNode.model_validate(menu) for menu in visible_menus}
    roots: list[MenuNode] = []

    for menu in visible_menus:
        node = nodes[menu.id]
        if menu.parent_id is None:
            roots.append(node)
            continue

        parent_node = nodes.get(menu.parent_id)
        if parent_node is None:
            roots.append(node)
        else:
            parent_node.children.append(node)

    return roots


async def update_menu(
    session: AsyncSession,
    menu_id: int,
    payload: MenuUpdate,
) -> Menu:
    menu = await _require_menu(session, menu_id)
    changes = payload.model_dump(exclude_unset=True)

    for field_name in _NON_NULLABLE_UPDATE_FIELDS:
        if field_name in changes and changes[field_name] is None:
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                f"{field_name} cannot be null",
            )

    new_type = changes.get("type", menu.type)
    new_parent_id = changes.get("parent_id", menu.parent_id)
    parent = await _require_parent(session, new_parent_id)
    _validate_parent(new_type, parent)
    await _ensure_parent_is_not_descendant(session, menu.id, new_parent_id)
    await _validate_children(session, menu, new_type)
    await _ensure_permission_code_available(
        session,
        changes.get("permission_code", menu.permission_code),
        exclude_id=menu.id,
    )

    for field_name, value in changes.items():
        setattr(menu, field_name, value)

    await session.commit()
    await session.refresh(menu)
    return menu


async def delete_menu(session: AsyncSession, menu_id: int) -> None:
    menu = await _require_menu(session, menu_id)
    if await menu_repository.has_children(session, menu_id):
        raise BusinessError(
            ErrorCode.INVALID_MENU_HIERARCHY,
            "Menu has children",
        )

    await session.delete(menu)
    await session.commit()


async def list_enabled_permission_codes(
    session: AsyncSession,
) -> frozenset[str]:
    all_menus = await menu_repository.list_all(session, include_disabled=True)
    menus_by_id = {menu.id: menu for menu in all_menus}
    return frozenset(
        menu.permission_code
        for menu in all_menus
        if menu.permission_code is not None
        and _is_effectively_enabled(menu, menus_by_id)
    )


__all__: tuple[str, ...] = (
    "create_menu",
    "delete_menu",
    "get_menu_tree",
    "list_enabled_permission_codes",
    "update_menu",
)
