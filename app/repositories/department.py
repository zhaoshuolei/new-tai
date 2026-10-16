"""部门数据访问。"""

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.department import Department, DepartmentStatus


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


def _has_user_table(sync_session: Session) -> bool:
    return inspect(sync_session.get_bind()).has_table("user")


async def has_users(session: AsyncSession, department_id: int) -> bool:
    if not await session.run_sync(_has_user_table):
        return False

    statement = text(
        """
        SELECT EXISTS (
            SELECT 1
            FROM `user`
            WHERE department_id = :department_id
        )
        """
    )
    return bool(
        await session.scalar(
            statement,
            {"department_id": department_id},
        )
    )


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


__all__: tuple[str, ...] = (
    "add",
    "get_by_id",
    "has_children",
    "has_users",
    "list_all",
    "list_by_path_prefix",
)
