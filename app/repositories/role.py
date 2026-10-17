"""角色数据访问。"""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.models.menu import Menu
from app.models.role import Role, RoleMenu
from app.models.role_department import RoleDepartment
from app.models.user import User


async def get_by_id(
    session: AsyncSession,
    role_id: int,
    *,
    include_deleted: bool = False,
) -> Role | None:
    statement = select(Role).where(Role.id == role_id)
    if not include_deleted:
        statement = statement.where(Role.deleted_at.is_(None))
    return await session.scalar(statement)


async def lock_by_id(
    session: AsyncSession,
    role_id: int,
    *,
    for_share: bool = False,
    include_deleted: bool = False,
) -> Role | None:
    """按主键锁定角色行，供删除或分配操作串行化使用。"""
    statement = select(Role).where(Role.id == role_id)
    if not include_deleted:
        statement = statement.where(Role.deleted_at.is_(None))
    statement = statement.with_for_update(read=for_share).execution_options(
        populate_existing=True
    )
    return await session.scalar(statement)


async def get_by_code(session: AsyncSession, code: str) -> Role | None:
    statement = select(Role).where(
        Role.code == code,
        Role.deleted_at.is_(None),
    )
    return await session.scalar(statement)


async def add(session: AsyncSession, role: Role) -> Role:
    session.add(role)
    await session.flush()
    return role


async def list_all(session: AsyncSession) -> list[Role]:
    statement = select(Role).where(Role.deleted_at.is_(None)).order_by(Role.id)
    result = await session.scalars(statement)
    return list(result.all())


async def list_deleted(session: AsyncSession) -> list[Role]:
    statement = (
        select(Role)
        .where(Role.deleted_at.is_not(None))
        .order_by(Role.deleted_at.desc(), Role.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_menus_by_ids(
    session: AsyncSession,
    menu_ids: set[int],
) -> list[Menu]:
    if not menu_ids:
        return []
    statement = select(Menu).where(
        Menu.id.in_(menu_ids),
        Menu.deleted_at.is_(None),
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_assigned_menu_ids(
    session: AsyncSession,
    role_id: int,
) -> list[int]:
    statement = (
        select(RoleMenu.c.menu_id)
        .join(Menu, Menu.id == RoleMenu.c.menu_id)
        .where(
            RoleMenu.c.role_id == role_id,
            Menu.deleted_at.is_(None),
        )
        .order_by(RoleMenu.c.menu_id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_assigned_department_ids(
    session: AsyncSession,
    role_id: int,
) -> list[int]:
    statement = (
        select(RoleDepartment.c.department_id)
        .join(Department, Department.id == RoleDepartment.c.department_id)
        .where(
            RoleDepartment.c.role_id == role_id,
            Department.deleted_at.is_(None),
        )
        .order_by(RoleDepartment.c.department_id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_assigned_permission_codes(
    session: AsyncSession,
    role_id: int,
) -> frozenset[str]:
    statement = (
        select(Menu.permission_code)
        .join(RoleMenu, RoleMenu.c.menu_id == Menu.id)
        .where(
            RoleMenu.c.role_id == role_id,
            Menu.deleted_at.is_(None),
        )
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
    statement = select(Department).where(
        Department.id.in_(department_ids),
        Department.deleted_at.is_(None),
    )
    result = await session.scalars(statement)
    return list(result.all())


async def lock_departments_by_ids(
    session: AsyncSession,
    department_ids: set[int],
) -> list[Department]:
    """按主键升序共享锁定部门，阻止删除与数据范围写入交叉。"""
    if not department_ids:
        return []
    statement = (
        select(Department)
        .where(
            Department.id.in_(sorted(department_ids)),
            Department.deleted_at.is_(None),
        )
        .order_by(Department.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
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


async def has_users(session: AsyncSession, role_id: int) -> bool:
    statement = (
        select(User.id)
        .where(
            User.role_id == role_id,
            User.deleted_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    return await session.scalar(statement) is not None


async def soft_delete(
    session: AsyncSession,
    role: Role,
    *,
    deleted_by: int,
) -> None:
    role.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    role.deleted_by = deleted_by


async def restore(session: AsyncSession, role: Role) -> None:
    role.deleted_at = None
    role.deleted_by = None


__all__: tuple[str, ...] = (
    "add",
    "get_by_code",
    "get_by_id",
    "has_users",
    "list_all",
    "list_assigned_department_ids",
    "list_assigned_menu_ids",
    "list_assigned_permission_codes",
    "list_deleted",
    "list_departments_by_ids",
    "list_menus_by_ids",
    "lock_by_id",
    "lock_departments_by_ids",
    "replace_role_departments",
    "replace_role_menus",
    "restore",
    "soft_delete",
)
