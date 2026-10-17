"""部门数据访问。"""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department, DepartmentStatus
from app.models.role import Role
from app.models.role_department import RoleDepartment
from app.models.user import User


async def get_by_id(
    session: AsyncSession,
    department_id: int,
    *,
    include_deleted: bool = False,
) -> Department | None:
    statement = select(Department).where(Department.id == department_id)
    if not include_deleted:
        statement = statement.where(Department.deleted_at.is_(None))
    return await session.scalar(statement)


async def lock_by_id(
    session: AsyncSession,
    department_id: int,
    *,
    for_share: bool = False,
    include_deleted: bool = False,
) -> Department | None:
    statement = select(Department).where(Department.id == department_id)
    if not include_deleted:
        statement = statement.where(Department.deleted_at.is_(None))
    statement = statement.with_for_update(read=for_share).execution_options(
        populate_existing=True
    )
    return await session.scalar(statement)


async def add(session: AsyncSession, department: Department) -> Department:
    session.add(department)
    await session.flush()
    return department


async def list_all(
    session: AsyncSession,
    *,
    include_disabled: bool,
) -> list[Department]:
    statement = select(Department).where(Department.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(Department.status == DepartmentStatus.ACTIVE)
    statement = statement.order_by(Department.sort_order, Department.id)
    result = await session.scalars(statement)
    return list(result.all())


async def list_deleted(session: AsyncSession) -> list[Department]:
    statement = (
        select(Department)
        .where(Department.deleted_at.is_not(None))
        .order_by(Department.deleted_at.desc(), Department.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def has_children(session: AsyncSession, department_id: int) -> bool:
    statement = (
        select(Department.id)
        .where(
            Department.parent_id == department_id,
            Department.deleted_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    return await session.scalar(statement) is not None


async def has_users(session: AsyncSession, department_id: int) -> bool:
    statement = (
        select(User.id)
        .where(
            User.department_id == department_id,
            User.deleted_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    return await session.scalar(statement) is not None


async def has_role_data_scope_references(
    session: AsyncSession,
    department_id: int,
) -> bool:
    statement = (
        select(RoleDepartment.c.role_id)
        .join(Role, Role.id == RoleDepartment.c.role_id)
        .where(
            RoleDepartment.c.department_id == department_id,
            Role.deleted_at.is_(None),
        )
        .limit(1)
        .with_for_update(of=RoleDepartment)
    )
    return await session.scalar(statement) is not None


async def list_by_path_prefix(
    session: AsyncSession,
    path_prefix: str,
) -> list[Department]:
    statement = (
        select(Department)
        .where(
            Department.path.startswith(path_prefix),
            Department.deleted_at.is_(None),
        )
        .order_by(Department.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def cascade_path_prefix(
    session: AsyncSession,
    *,
    old_prefix: str,
    new_prefix: str,
    exclude_id: int,
) -> None:
    loaded_descendant_ids = [
        item.id
        for item in session.identity_map.values()
        if isinstance(item, Department)
        and item.id != exclude_id
        and item.path.startswith(old_prefix)
    ]
    suffix = func.substr(Department.path, len(old_prefix) + 1)
    statement = (
        update(Department)
        .where(
            Department.path.startswith(old_prefix),
            Department.id != exclude_id,
        )
        .values(path=func.concat(new_prefix, suffix))
        .execution_options(synchronize_session=False)
    )
    await session.execute(statement)

    if loaded_descendant_ids:
        refresh_statement = (
            select(Department)
            .where(Department.id.in_(loaded_descendant_ids))
            .execution_options(populate_existing=True)
        )
        await session.scalars(refresh_statement)


async def soft_delete(
    session: AsyncSession,
    department: Department,
    *,
    deleted_by: int | None,
) -> None:
    department.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    department.deleted_by = deleted_by


async def restore(session: AsyncSession, department: Department) -> None:
    department.deleted_at = None
    department.deleted_by = None


__all__: tuple[str, ...] = (
    "add",
    "cascade_path_prefix",
    "get_by_id",
    "has_children",
    "has_role_data_scope_references",
    "has_users",
    "list_all",
    "list_by_path_prefix",
    "list_deleted",
    "lock_by_id",
    "restore",
    "soft_delete",
)
