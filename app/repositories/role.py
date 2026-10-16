"""角色数据访问。"""

from collections.abc import Iterable

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.models.department import Department
from app.models.menu import Menu
from app.models.role import Role, RoleMenu
from app.models.role_department import RoleDepartment


async def get_by_id(session: AsyncSession, role_id: int) -> Role | None:
    return await session.get(Role, role_id)


async def lock_by_id(session: AsyncSession, role_id: int) -> Role | None:
    """按主键锁定角色行，供整集合替换串行化使用。"""
    statement = (
        select(Role)
        .where(Role.id == role_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def get_by_code(session: AsyncSession, code: str) -> Role | None:
    statement = select(Role).where(Role.code == code)
    return await session.scalar(statement)


async def add(session: AsyncSession, role: Role) -> Role:
    session.add(role)
    await session.flush()
    return role


async def list_all(session: AsyncSession) -> list[Role]:
    statement = select(Role).order_by(Role.id)
    result = await session.scalars(statement)
    return list(result.all())


async def list_menus_by_ids(
    session: AsyncSession,
    menu_ids: set[int],
) -> list[Menu]:
    if not menu_ids:
        return []
    statement = select(Menu).where(Menu.id.in_(menu_ids))
    result = await session.scalars(statement)
    return list(result.all())


async def list_assigned_permission_codes(
    session: AsyncSession,
    role_id: int,
) -> frozenset[str]:
    statement = (
        select(Menu.permission_code)
        .join(RoleMenu, RoleMenu.c.menu_id == Menu.id)
        .where(RoleMenu.c.role_id == role_id)
    )
    result = await session.scalars(statement)
    return frozenset(code for code in result.all() if code is not None)


async def replace_role_menus(
    session: AsyncSession,
    role_id: int,
    menu_ids: set[int],
) -> None:
    await session.execute(delete(RoleMenu).where(RoleMenu.c.role_id == role_id))
    if menu_ids:
        await session.execute(
            insert(RoleMenu),
            [{"role_id": role_id, "menu_id": menu_id} for menu_id in sorted(menu_ids)],
        )


async def list_departments_by_ids(
    session: AsyncSession,
    department_ids: set[int],
) -> list[Department]:
    if not department_ids:
        return []
    statement = select(Department).where(Department.id.in_(department_ids))
    result = await session.scalars(statement)
    return list(result.all())


async def replace_role_departments(
    session: AsyncSession,
    role_id: int,
    department_ids: Iterable[int],
) -> None:
    await session.execute(
        delete(RoleDepartment).where(RoleDepartment.c.role_id == role_id)
    )
    ordered_ids = sorted(set(department_ids))
    if ordered_ids:
        await session.execute(
            insert(RoleDepartment),
            [
                {"role_id": role_id, "department_id": department_id}
                for department_id in ordered_ids
            ],
        )


def _user_table_is_defined() -> bool:
    """判断用户模型是否已注册；当前角色任务阶段该表尚未创建。"""
    return "user" in Base.metadata.tables


async def has_users(session: AsyncSession, role_id: int) -> bool:
    # TODO（Task 9 用户模型与本地账号 CRUD）：当前 user 表尚未创建，这里是
    # ROLE_IN_USE 的临时守卫。Task 9 必须替换为真实 User.role_id 外键
    # ON DELETE RESTRICT，将外键删除异常映射为 ROLE_IN_USE=3002，并补充
    # “删除被用户引用的角色失败”的真实数据库集成测试。
    if not _user_table_is_defined():
        return False

    statement = text(
        """
        SELECT EXISTS (
            SELECT 1
            FROM `user`
            WHERE role_id = :role_id
        )
        """
    )
    return bool(
        await session.scalar(
            statement,
            {"role_id": role_id},
        )
    )


__all__: tuple[str, ...] = (
    "add",
    "get_by_code",
    "get_by_id",
    "has_users",
    "list_all",
    "list_assigned_permission_codes",
    "list_departments_by_ids",
    "list_menus_by_ids",
    "lock_by_id",
    "replace_role_departments",
    "replace_role_menus",
)
