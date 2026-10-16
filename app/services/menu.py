"""菜单树业务逻辑。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.core.permissions import (
    invalidate_role_permissions,
    invalidate_super_admin_permissions,
)
from app.core.redis import redis_scope
from app.models.menu import Menu, MenuStatus, MenuType
from app.models.role import RoleStatus
from app.repositories import menu as menu_repository
from app.repositories import role as role_repository
from app.schemas.menu import MenuCreate, MenuNode, MenuUpdate

_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"type", "name", "sort_order", "visible", "status"}
)


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


async def _lock_menu_candidates(
    session: AsyncSession,
    root_ids: set[int],
) -> dict[int, Menu]:
    # 所有菜单写操作先汇总目标、新旧父节点和祖先，再由 lock_menu_nodes
    # 单次按 id 升序 FOR UPDATE；统一顺序避免父子 id 大小相反时交叉等待。
    candidate_ids = set(root_ids)
    for root_id in root_ids:
        await _collect_ancestor_ids(session, root_id, candidate_ids)

    return await menu_repository.lock_menu_nodes(session, candidate_ids)


async def _lock_children_for_validation(
    session: AsyncSession,
    menu_id: int,
) -> list[Menu]:
    # 目标行已锁定后才锁读直接子节点；并发跨层写即使触发死锁，也会回滚并
    # 映射为可重试的 409，绝不牺牲菜单层级不变量。
    return await menu_repository.list_children_for_update(session, menu_id)


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


def _validate_children(
    children: list[Menu],
    menu_type: MenuType,
) -> None:
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


def _mysql_error_number(error: IntegrityError | OperationalError) -> int | None:
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


def _raise_mapped_operational_error(error: OperationalError) -> NoReturn:
    if _mysql_error_number(error) in {1205, 1213}:
        raise BusinessError(
            ErrorCode.CONFLICT,
            "菜单正在被并发修改，请稍后重试",
            http_status=409,
        )
    raise error


@asynccontextmanager
async def _map_menu_write_errors(
    session: AsyncSession,
) -> AsyncIterator[None]:
    try:
        yield
    except IntegrityError as error:
        await session.rollback()
        _raise_mapped_integrity_error(error)
    except OperationalError as error:
        await session.rollback()
        _raise_mapped_operational_error(error)


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


def _is_effectively_visible(menu: Menu, menus_by_id: dict[int, Menu]) -> bool:
    current: Menu | None = menu
    visited: set[int] = set()
    while current is not None:
        if current.id in visited or not current.visible:
            return False
        visited.add(current.id)
        if current.parent_id is None:
            return True
        current = menus_by_id.get(current.parent_id)
    return False


def _build_menu_tree(menus: list[Menu]) -> list[MenuNode]:
    nodes = {menu.id: MenuNode.model_validate(menu) for menu in menus}
    roots: list[MenuNode] = []

    for menu in menus:
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


async def _invalidate_permission_caches_after_menu_write(
    session: AsyncSession,
    redis: Redis | None,
) -> None:
    async with redis_scope(redis) as resolved_redis:
        await invalidate_super_admin_permissions(resolved_redis)
        for role in await role_repository.list_all(session):
            await invalidate_role_permissions(resolved_redis, role.id)


async def create_menu(
    session: AsyncSession,
    payload: MenuCreate,
    *,
    redis: Redis | None = None,
) -> Menu:
    async with _map_menu_write_errors(session):
        root_ids = {payload.parent_id} if payload.parent_id is not None else set()
        locked = await _lock_menu_candidates(session, root_ids)
        parent = (
            locked.get(payload.parent_id) if payload.parent_id is not None else None
        )
        if payload.parent_id is not None and parent is None:
            raise BusinessError(
                ErrorCode.MENU_NOT_FOUND,
                "Menu not found",
                http_status=404,
            )
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
        await _invalidate_permission_caches_after_menu_write(session, redis)
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
    return _build_menu_tree(visible_menus)


async def get_visible_menu_tree(
    session: AsyncSession,
    *,
    role_id: int | None,
    is_super_admin: bool,
) -> list[MenuNode]:
    """返回当前用户可见的非按钮菜单树。"""
    all_menus = await menu_repository.list_all(session, include_disabled=True)
    menus_by_id = {menu.id: menu for menu in all_menus}
    visible_ids: set[int] = set()

    if is_super_admin:
        visible_ids = {
            menu.id
            for menu in all_menus
            if menu.type != MenuType.BUTTON
            and _is_effectively_enabled(menu, menus_by_id)
            and _is_effectively_visible(menu, menus_by_id)
        }
    elif role_id is not None:
        role = await role_repository.get_by_id(session, role_id)
        if role is None or role.status != RoleStatus.ACTIVE:
            return []

        assigned_ids = await menu_repository.list_assigned_menu_ids(session, role_id)
        for assigned_id in assigned_ids:
            menu = menus_by_id.get(assigned_id)
            if menu is None or not _is_effectively_enabled(menu, menus_by_id):
                continue
            if not _is_effectively_visible(menu, menus_by_id):
                continue

            current: Menu | None = menu
            while current is not None:
                if current.type != MenuType.BUTTON:
                    visible_ids.add(current.id)
                parent_id = current.parent_id
                current = menus_by_id.get(parent_id) if parent_id is not None else None

    visible_menus = [menu for menu in all_menus if menu.id in visible_ids]
    return _build_menu_tree(visible_menus)


async def update_menu(
    session: AsyncSession,
    menu_id: int,
    payload: MenuUpdate,
    *,
    redis: Redis | None = None,
) -> Menu:
    async with _map_menu_write_errors(session):
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
        locked = await _lock_menu_candidates(session, root_ids)

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
        children = await _lock_children_for_validation(session, menu.id)
        _validate_parent(new_type, parent)
        _ensure_parent_is_not_descendant(menu.id, new_parent_id, locked)
        _validate_children(children, new_type)
        await _ensure_permission_code_available(
            session,
            changes.get("permission_code", menu.permission_code),
            exclude_id=menu.id,
        )

        for field_name, value in changes.items():
            setattr(menu, field_name, value)

        await session.commit()
        await session.refresh(menu)
        await _invalidate_permission_caches_after_menu_write(session, redis)
        return menu


async def delete_menu(
    session: AsyncSession,
    menu_id: int,
    *,
    redis: Redis | None = None,
) -> None:
    async with _map_menu_write_errors(session):
        locked = await _lock_menu_candidates(session, {menu_id})
        menu = locked.get(menu_id)
        if menu is None:
            raise BusinessError(
                ErrorCode.MENU_NOT_FOUND,
                "Menu not found",
                http_status=404,
            )
        children = await _lock_children_for_validation(session, menu.id)
        if children:
            raise BusinessError(
                ErrorCode.INVALID_MENU_HIERARCHY,
                "Menu has children",
            )

        await session.delete(menu)
        await session.commit()
        await _invalidate_permission_caches_after_menu_write(session, redis)


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
    "get_visible_menu_tree",
    "list_enabled_permission_codes",
    "update_menu",
)
