"""菜单数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import Menu, MenuStatus
from app.models.role import RoleMenu


async def get_by_id(session: AsyncSession, menu_id: int) -> Menu | None:
    return await session.get(Menu, menu_id)


async def lock_menu_nodes(
    session: AsyncSession,
    menu_ids: set[int],
) -> dict[int, Menu]:
    """一次按主键升序锁定全部候选菜单行。"""
    if not menu_ids:
        return {}

    ordered_ids = sorted(menu_ids)
    statement = (
        select(Menu)
        .where(Menu.id.in_(ordered_ids))
        .order_by(Menu.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(statement)
    return {menu.id: menu for menu in result.all()}


async def get_by_permission_code(
    session: AsyncSession,
    permission_code: str,
) -> Menu | None:
    statement = select(Menu).where(Menu.permission_code == permission_code)
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
    statement = select(Menu)
    if not include_disabled:
        statement = statement.where(Menu.status == MenuStatus.ACTIVE)
    statement = statement.order_by(Menu.sort_order, Menu.id)
    result = await session.scalars(statement)
    return list(result.all())


async def list_children(session: AsyncSession, menu_id: int) -> list[Menu]:
    statement = (
        select(Menu).where(Menu.parent_id == menu_id).order_by(Menu.sort_order, Menu.id)
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
        .where(Menu.parent_id == menu_id)
        .order_by(Menu.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def has_children(session: AsyncSession, menu_id: int) -> bool:
    statement = select(Menu.id).where(Menu.parent_id == menu_id).limit(1)
    return await session.scalar(statement) is not None


__all__: tuple[str, ...] = (
    "add",
    "get_by_id",
    "get_by_permission_code",
    "has_children",
    "list_all",
    "list_assigned_menu_ids",
    "list_children",
    "list_children_for_update",
    "lock_menu_nodes",
)
