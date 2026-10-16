"""部门数据访问。"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
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


def _user_table_is_defined() -> bool:
    """判断用户模型是否已注册；当前部门任务阶段该表尚未创建。"""
    return "user" in Base.metadata.tables


async def has_users(session: AsyncSession, department_id: int) -> bool:
    # TODO（Task 9 用户模型与本地账号 CRUD）：当前任务阶段 user 表尚未创建，
    # 因此仅在 ORM 元数据已注册 user 表时执行引用检查，避免提前依赖未来 schema。
    # 后续用户任务必须改为依赖真实 user.department_id 外键 ON DELETE RESTRICT，
    # 将外键约束冲突映射为 DEPT_HAS_USERS=5002，并补充真实数据库集成测试。
    # 若 user 模型已注册但数据库表缺失，本函数不吞掉查询异常，以暴露 schema drift。
    if not _user_table_is_defined():
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
