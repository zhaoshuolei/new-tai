"""部门树业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department, DepartmentStatus
from app.repositories import department as department_repository
from app.schemas.department import (
    DepartmentCreate,
    DepartmentNode,
    DepartmentUpdate,
)


async def _require_department(
    session: AsyncSession,
    department_id: int,
) -> Department:
    department = await department_repository.get_by_id(session, department_id)
    if department is None:
        raise BusinessError(
            ErrorCode.DEPT_NOT_FOUND,
            "Department not found",
            http_status=404,
        )
    return department


async def create_department(
    session: AsyncSession,
    payload: DepartmentCreate,
) -> Department:
    parent: Department | None = None
    if payload.parent_id is not None:
        parent = await _require_department(session, payload.parent_id)
        if parent.status == DepartmentStatus.DISABLED:
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                "Cannot create a child under a disabled department",
            )

    department = Department(
        parent_id=payload.parent_id,
        name=payload.name,
        path="",
        sort_order=payload.sort_order,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    await department_repository.add(session, department)
    parent_path = parent.path if parent is not None else "/"
    department.path = f"{parent_path}{department.id}/"
    await session.commit()
    await session.refresh(department)
    return department


async def get_tree(
    session: AsyncSession,
    include_disabled: bool = False,
) -> list[DepartmentNode]:
    departments = await department_repository.list_all(
        session,
        include_disabled=include_disabled,
    )
    nodes = {
        department.id: DepartmentNode.model_validate(department)
        for department in departments
    }
    roots: list[DepartmentNode] = []

    for department in departments:
        node = nodes[department.id]
        parent = nodes.get(department.parent_id) if department.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    return roots


async def update_department(
    session: AsyncSession,
    department_id: int,
    payload: DepartmentUpdate,
) -> Department:
    department = await _require_department(session, department_id)
    if department.is_system:
        raise BusinessError(
            ErrorCode.SYSTEM_DEPT_IMMUTABLE,
            "System department is immutable",
        )

    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(department, field_name, value)

    await session.commit()
    await session.refresh(department)
    return department


async def delete_department(
    session: AsyncSession,
    department_id: int,
) -> None:
    department = await _require_department(session, department_id)
    if department.is_system:
        raise BusinessError(
            ErrorCode.SYSTEM_DEPT_IMMUTABLE,
            "System department is immutable",
        )
    if await department_repository.has_children(session, department_id):
        raise BusinessError(
            ErrorCode.DEPT_HAS_CHILDREN,
            "Department has children",
        )
    if await department_repository.has_users(session, department_id):
        raise BusinessError(
            ErrorCode.DEPT_HAS_USERS,
            "Department has users",
        )

    await session.delete(department)
    await session.commit()


async def list_subtree_ids(
    session: AsyncSession,
    department_id: int,
) -> list[int]:
    department = await _require_department(session, department_id)
    descendants = await department_repository.list_by_path_prefix(
        session,
        department.path,
    )
    return [item.id for item in descendants]


__all__: tuple[str, ...] = (
    "create_department",
    "delete_department",
    "get_tree",
    "list_subtree_ids",
    "update_department",
)
