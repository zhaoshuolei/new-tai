"""菜单数据访问。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import Menu, MenuStatus
from app.models.role import RoleMenu


async def get_by_id(
    session: AsyncSession,
    menu_id: int,
    *,
    include_deleted: bool = False,
) -> Menu | None:
    statement = select(Menu).where(Menu.id == menu_id)
    if not include_deleted:
        statement = statement.where(Menu.deleted_at.is_(None))
    return await session.scalar(statement)


async def lock_by_id(
    session: AsyncSession,
    menu_id: int,
    *,
    include_deleted: bool = False,
) -> Menu | None:
    statement = select(Menu).where(Menu.id == menu_id)
    if not include_deleted:
        statement = statement.where(Menu.deleted_at.is_(None))
    statement = statement.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(statement)


async def lock_menu_nodes(
    session: AsyncSession,
    menu_ids: set[int],
    *,
    include_deleted: bool = False,
) -> dict[int, Menu]:
    """一次按主键升序锁定全部候选菜单行。"""
    if not menu_ids:
        return {}

    ordered_ids = sorted(menu_ids)
    statement = select(Menu).where(Menu.id.in_(ordered_ids))
    if not include_deleted:
        statement = statement.where(Menu.deleted_at.is_(None))
    statement = (
        statement.order_by(Menu.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(statement)
    return {menu.id: menu for menu in result.all()}


async def get_by_permission_code(
    session: AsyncSession,
    permission_code: str,
) -> Menu | None:
    statement = select(Menu).where(
        Menu.permission_code == permission_code,
        Menu.deleted_at.is_(None),
    )
    return await session.scalar(statement)


async def add(session: AsyncSession, menu: Menu) -> Menu:
    session.add(menu)
    await session.flush()
    return menu


async def list_all(
    session: AsyncSession,
    *,
    include_disabled: bool,
) -> list[Menu]:
    statement = select(Menu).where(Menu.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(Menu.status == MenuStatus.ACTIVE)
    statement = statement.order_by(Menu.sort_order, Menu.id)
    result = await session.scalars(statement)
    return list(result.all())


async def list_deleted(session: AsyncSession) -> list[Menu]:
    statement = (
        select(Menu)
        .where(Menu.deleted_at.is_not(None))
        .order_by(Menu.deleted_at.desc(), Menu.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_children(session: AsyncSession, menu_id: int) -> list[Menu]:
    statement = (
        select(Menu)
        .where(
            Menu.parent_id == menu_id,
            Menu.deleted_at.is_(None),
        )
        .order_by(Menu.sort_order, Menu.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_assigned_menu_ids(
    session: AsyncSession,
    role_id: int,
) -> frozenset[int]:
    statement = select(RoleMenu.c.menu_id).where(RoleMenu.c.role_id == role_id)
    result = await session.scalars(statement)
    return frozenset(result.all())


async def list_children_for_update(
    session: AsyncSession,
    menu_id: int,
) -> list[Menu]:
    """按 id 升序锁定并读取直接子节点。"""
    statement = (
        select(Menu)
        .where(
            Menu.parent_id == menu_id,
            Menu.deleted_at.is_(None),
        )
        .order_by(Menu.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def has_children(session: AsyncSession, menu_id: int) -> bool:
    statement = (
        select(Menu.id)
        .where(
            Menu.parent_id == menu_id,
            Menu.deleted_at.is_(None),
        )
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def soft_delete(
    session: AsyncSession,
    menu: Menu,
    *,
    deleted_by: int | None,
) -> None:
    menu.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    menu.deleted_by = deleted_by


async def restore(session: AsyncSession, menu: Menu) -> None:
    menu.deleted_at = None
    menu.deleted_by = None


__all__: tuple[str, ...] = (
    "add",
    "get_by_id",
    "get_by_permission_code",
    "has_children",
    "list_all",
    "list_assigned_menu_ids",
    "list_children",
    "list_children_for_update",
    "list_deleted",
    "lock_by_id",
    "lock_menu_nodes",
    "restore",
    "soft_delete",
)
