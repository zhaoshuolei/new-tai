"""菜单树业务逻辑。"""

from typing import NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.menu import Menu, MenuStatus, MenuType
from app.repositories import menu as menu_repository
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate

_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"type", "name", "sort_order", "visible", "status"}
)


async def _require_menu_for_update(
    session: AsyncSession,
    menu_id: int,
) -> Menu:
    menu = await menu_repository.get_by_id_for_update(session, menu_id)
    if menu is None:
        raise BusinessError(
            ErrorCode.MENU_NOT_FOUND,
            "Menu not found",
            http_status=404,
        )
    return menu


async def _require_parent_for_update(
    session: AsyncSession,
    parent_id: int | None,
) -> Menu | None:
    if parent_id is None:
        return None
    parent = await menu_repository.get_by_id_for_update(session, parent_id)
    if parent is None:
        raise BusinessError(
            ErrorCode.MENU_NOT_FOUND,
            "Menu not found",
            http_status=404,
        )
    return parent


async def _collect_ancestor_ids(
    session: AsyncSession,
    menu_id: int,
    menu_ids: set[int],
) -> None:
    current_id: int | None = menu_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        menu_ids.add(current_id)
        menu = await menu_repository.get_by_id(session, current_id)
        if menu is None:
            return
        current_id = menu.parent_id


async def _lock_menu_graph(
    session: AsyncSession,
    root_ids: set[int],
) -> dict[int, Menu]:
    # 菜单写操作统一按 id 升序 FOR UPDATE，覆盖目标、新旧父节点和祖先链，
    # 避免并发重挂形成环，并降低交叉加锁造成死锁的风险。
    lock_ids = set(root_ids)
    for root_id in root_ids:
        await _collect_ancestor_ids(session, root_id, lock_ids)

    while True:
        locked = await menu_repository.lock_by_ids(session, lock_ids)
        expanded_ids = set(lock_ids)
        for menu in locked.values():
            if menu.parent_id is not None:
                expanded_ids.add(menu.parent_id)
        if expanded_ids == lock_ids:
            return locked
        lock_ids = expanded_ids


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
    children = await menu_repository.list_children(
        session,
        menu.id,
        for_update=True,
    )
    for child in children:
        if not _is_valid_parent_type(child.type, menu_type):
            raise BusinessError(
                ErrorCode.INVALID_MENU_HIERARCHY,
                "Invalid menu hierarchy",
            )


def _ensure_parent_is_not_descendant(
    menu_id: int,
    parent_id: int | None,
    menus_by_id: dict[int, Menu],
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
        ancestor = menus_by_id.get(ancestor_id)
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


def _mysql_error_number(error: IntegrityError) -> int | None:
    args = getattr(error.orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _raise_mapped_integrity_error(error: IntegrityError) -> NoReturn:
    message = str(error.orig).lower()
    if _mysql_error_number(error) == 1062 and (
        "menu.ix_menu_permission_code" in message or "permission_code" in message
    ):
        raise BusinessError(
            ErrorCode.PERMISSION_CODE_EXISTS,
            "Permission code already exists",
            http_status=409,
        )
    if (
        _mysql_error_number(error) in {1451, 1452}
        and "foreign key" in message
        and "`menu`" in message
    ):
        raise BusinessError(
            ErrorCode.INVALID_MENU_HIERARCHY,
            "Invalid menu hierarchy",
        )
    raise error


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
    parent = await _require_parent_for_update(session, payload.parent_id)
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
    try:
        await menu_repository.add(session, menu)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        _raise_mapped_integrity_error(error)
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
    changes = payload.model_dump(exclude_unset=True)

    for field_name in _NON_NULLABLE_UPDATE_FIELDS:
        if field_name in changes and changes[field_name] is None:
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                f"{field_name} cannot be null",
            )

    root_ids = {menu_id}
    requested_parent_id = changes.get("parent_id", None)
    if requested_parent_id is not None:
        root_ids.add(requested_parent_id)
    locked = await _lock_menu_graph(session, root_ids)

    menu = locked.get(menu_id)
    if menu is None:
        raise BusinessError(
            ErrorCode.MENU_NOT_FOUND,
            "Menu not found",
            http_status=404,
        )

    new_type = changes.get("type", menu.type)
    new_parent_id = changes.get("parent_id", menu.parent_id)
    parent = locked.get(new_parent_id) if new_parent_id is not None else None
    if new_parent_id is not None and parent is None:
        raise BusinessError(
            ErrorCode.MENU_NOT_FOUND,
            "Menu not found",
            http_status=404,
        )
    _validate_parent(new_type, parent)
    _ensure_parent_is_not_descendant(menu.id, new_parent_id, locked)
    await _validate_children(session, menu, new_type)
    await _ensure_permission_code_available(
        session,
        changes.get("permission_code", menu.permission_code),
        exclude_id=menu.id,
    )

    for field_name, value in changes.items():
        setattr(menu, field_name, value)

    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        _raise_mapped_integrity_error(error)
    await session.refresh(menu)
    return menu


async def delete_menu(session: AsyncSession, menu_id: int) -> None:
    # 先锁定目标行，再检查子节点；并发创建子节点会先等待父节点锁释放。
    menu = await _require_menu_for_update(session, menu_id)
    if await menu_repository.has_children(session, menu_id):
        raise BusinessError(
            ErrorCode.INVALID_MENU_HIERARCHY,
            "Menu has children",
        )

    try:
        await session.delete(menu)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        _raise_mapped_integrity_error(error)


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
