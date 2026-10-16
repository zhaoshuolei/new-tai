"""菜单数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import Menu, MenuStatus


async def get_by_id(session: AsyncSession, menu_id: int) -> Menu | None:
    return await session.get(Menu, menu_id)


async def get_by_id_for_update(
    session: AsyncSession,
    menu_id: int,
) -> Menu | None:
    statement = (
        select(Menu)
        .where(Menu.id == menu_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def lock_by_ids(
    session: AsyncSession,
    menu_ids: set[int],
) -> dict[int, Menu]:
    """按主键升序锁定菜单行，调用方据此保持稳定的加锁顺序。"""
    if not menu_ids:
        return {}

    statement = (
        select(Menu)
        .where(Menu.id.in_(menu_ids))
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


async def list_children(
    session: AsyncSession,
    menu_id: int,
    *,
    for_update: bool = False,
) -> list[Menu]:
    statement = (
        select(Menu).where(Menu.parent_id == menu_id).order_by(Menu.sort_order, Menu.id)
    )
    if for_update:
        statement = statement.with_for_update()
    result = await session.scalars(statement)
    return list(result.all())


async def has_children(session: AsyncSession, menu_id: int) -> bool:
    statement = select(Menu.id).where(Menu.parent_id == menu_id).limit(1)
    return await session.scalar(statement) is not None


__all__: tuple[str, ...] = (
    "add",
    "get_by_id",
    "get_by_id_for_update",
    "get_by_permission_code",
    "has_children",
    "list_all",
    "list_children",
    "lock_by_ids",
)
