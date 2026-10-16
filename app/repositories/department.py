"""部门数据访问。"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department, DepartmentStatus
from app.models.user import User


async def get_by_id(
    session: AsyncSession,
    department_id: int,
) -> Department | None:
    return await session.get(Department, department_id)


async def add(session: AsyncSession, department: Department) -> Department:
    session.add(department)
    await session.flush()
    return department


async def list_all(
    session: AsyncSession,
    *,
    include_disabled: bool,
) -> list[Department]:
    statement = select(Department)
    if not include_disabled:
        statement = statement.where(Department.status == DepartmentStatus.ACTIVE)
    statement = statement.order_by(Department.sort_order, Department.id)
    result = await session.scalars(statement)
    return list(result.all())


async def has_children(session: AsyncSession, department_id: int) -> bool:
    statement = (
        select(Department.id)
        .where(
            Department.parent_id == department_id,
        )
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def has_users(session: AsyncSession, department_id: int) -> bool:
    statement = select(User.id).where(User.department_id == department_id).limit(1)
    return await session.scalar(statement) is not None


async def list_by_path_prefix(
    session: AsyncSession,
    path_prefix: str,
) -> list[Department]:
    statement = (
        select(Department)
        .where(Department.path.startswith(path_prefix))
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


__all__: tuple[str, ...] = (
    "add",
    "cascade_path_prefix",
    "get_by_id",
    "has_children",
    "has_users",
    "list_all",
    "list_by_path_prefix",
)
