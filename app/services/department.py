"""部门树业务逻辑。"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department, DepartmentStatus
from app.repositories import department as department_repository
from app.schemas.department import (
    DepartmentCreate,
    DepartmentNode,
    DepartmentUpdate,
)


def _is_user_delete_fk_error(error: IntegrityError) -> bool:
    args = getattr(error.orig, "args", ())
    return bool(
        args
        and args[0] == 1451
        and "foreign key" in str(error.orig).lower()
        and "`user`" in str(error.orig).lower()
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
    all_departments = await department_repository.list_all(
        session,
        include_disabled=True,
    )
    visible_departments = (
        all_departments
        if include_disabled
        else [
            department
            for department in all_departments
            if department.status == DepartmentStatus.ACTIVE
        ]
    )
    departments_by_id = {department.id: department for department in all_departments}
    nodes = {
        department.id: DepartmentNode.model_validate(department)
        for department in visible_departments
    }
    roots: list[DepartmentNode] = []

    for department in visible_departments:
        node = nodes[department.id]
        parent_node = _nearest_visible_ancestor(
            department,
            departments_by_id,
            nodes,
        )
        if parent_node is None:
            roots.append(node)
        else:
            parent_node.children.append(node)

    return roots


def _nearest_visible_ancestor(
    department: Department,
    departments_by_id: dict[int, Department],
    nodes: dict[int, DepartmentNode],
) -> DepartmentNode | None:
    ancestor_id = department.parent_id
    visited: set[int] = set()

    while ancestor_id is not None and ancestor_id not in visited:
        visited.add(ancestor_id)
        ancestor = departments_by_id.get(ancestor_id)
        if ancestor is None:
            return None

        parent_node = nodes.get(ancestor.id)
        if parent_node is not None:
            return parent_node

        ancestor_id = ancestor.parent_id

    return None


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


async def move_department(
    session: AsyncSession,
    dept_id: int,
    new_parent_id: int | None,
) -> Department:
    department = await _require_department(session, dept_id)
    if department.is_system:
        raise BusinessError(
            ErrorCode.SYSTEM_DEPT_IMMUTABLE,
            "System department is immutable",
        )

    parent: Department | None = None
    if new_parent_id is not None:
        parent = await _require_department(session, new_parent_id)

    if new_parent_id == dept_id:
        raise BusinessError(
            ErrorCode.INVALID_DEPT_MOVE,
            "Cannot move a department under itself",
        )

    if parent is not None and parent.path.startswith(department.path):
        raise BusinessError(
            ErrorCode.INVALID_DEPT_MOVE,
            "Cannot move a department under its descendant",
        )

    if parent is not None and parent.status == DepartmentStatus.DISABLED:
        raise BusinessError(
            ErrorCode.INVALID_DEPT_MOVE,
            "Cannot move a department under a disabled department",
        )

    old_path = department.path
    department.parent_id = new_parent_id
    department.path = f"{parent.path if parent is not None else '/'}{department.id}/"
    await session.flush()
    await department_repository.cascade_path_prefix(
        session,
        old_prefix=old_path,
        new_prefix=department.path,
        exclude_id=department.id,
    )
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
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if _is_user_delete_fk_error(error):
            raise BusinessError(
                ErrorCode.DEPT_HAS_USERS,
                "Department has users",
            ) from error
        raise


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
    "move_department",
    "update_department",
)
