"""部门树业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department, DepartmentStatus
from app.repositories import department as department_repository
from app.schemas.department import (
    DepartmentCreate,
    DepartmentNode,
    DepartmentUpdate,
)
from app.services.audit import audited


async def _require_department(
    session: AsyncSession,
    department_id: int,
    *,
    for_share: bool = False,
    for_update: bool = False,
    include_deleted: bool = False,
) -> Department:
    department = (
        await department_repository.lock_by_id(
            session,
            department_id,
            for_share=for_share,
            include_deleted=include_deleted,
        )
        if for_share or for_update
        else await department_repository.get_by_id(
            session,
            department_id,
            include_deleted=include_deleted,
        )
    )
    if department is None:
        raise BusinessError(
            ErrorCode.DEPT_NOT_FOUND,
            "Department not found",
            http_status=404,
        )
    return department


@audited(action="dept:create", resource_type="department", resource_id_attr="id")
async def create_department(
    session: AsyncSession,
    payload: DepartmentCreate,
    *,
    context: AuthContext | None = None,
) -> Department:
    parent: Department | None = None
    if payload.parent_id is not None:
        parent = await _require_department(
            session,
            payload.parent_id,
            for_share=True,
        )
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


@audited(
    action="dept:update",
    resource_type="department",
    resource_id_param="department_id",
)
async def update_department(
    session: AsyncSession,
    department_id: int,
    payload: DepartmentUpdate,
    *,
    context: AuthContext | None = None,
) -> Department:
    department = await _require_department(
        session,
        department_id,
        for_update=True,
    )
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


@audited(action="dept:move", resource_type="department", resource_id_param="dept_id")
async def move_department(
    session: AsyncSession,
    dept_id: int,
    new_parent_id: int | None,
    *,
    context: AuthContext | None = None,
) -> Department:
    department = await _require_department(
        session,
        dept_id,
        for_update=True,
    )
    if department.is_system:
        raise BusinessError(
            ErrorCode.SYSTEM_DEPT_IMMUTABLE,
            "System department is immutable",
        )

    parent: Department | None = None
    if new_parent_id is not None:
        parent = await _require_department(
            session,
            new_parent_id,
            for_share=True,
        )

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


@audited(
    action="dept:delete",
    resource_type="department",
    resource_id_param="department_id",
)
async def delete_department(
    session: AsyncSession,
    department_id: int,
    *,
    context: AuthContext | None = None,
) -> None:
    department = await _require_department(
        session,
        department_id,
        for_update=True,
    )
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
    if await department_repository.has_role_data_scope_references(
        session,
        department_id,
    ):
        raise BusinessError(
            ErrorCode.DEPT_REFERENCED_BY_ROLE,
            "部门被角色自定义数据范围引用，无法删除",
            http_status=409,
        )

    await department_repository.soft_delete(
        session,
        department,
        deleted_by=context.user_id if context is not None else None,
    )
    await session.commit()


@audited(
    action="dept:restore",
    resource_type="department",
    resource_id_param="department_id",
)
async def restore_department(
    session: AsyncSession,
    department_id: int,
    *,
    context: AuthContext | None = None,
) -> Department:
    department = await department_repository.lock_by_id(
        session,
        department_id,
        include_deleted=True,
    )
    if department is None or department.deleted_at is None:
        raise BusinessError(
            ErrorCode.RECORD_NOT_DELETED,
            "Department is not deleted",
            http_status=409,
        )
    parent: Department | None = None
    if department.parent_id is not None:
        parent = await department_repository.lock_by_id(
            session,
            department.parent_id,
            for_share=True,
        )
        if parent is None:
            raise BusinessError(
                ErrorCode.DEPT_NOT_FOUND,
                "Parent department is unavailable",
                http_status=409,
            )
        if parent.status == DepartmentStatus.DISABLED:
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                "Cannot restore a department under a disabled department",
            )
    old_path = department.path
    department.path = f"{parent.path if parent is not None else '/'}{department.id}/"
    await department_repository.restore(session, department)
    await session.flush()
    if old_path != department.path:
        await department_repository.cascade_path_prefix(
            session,
            old_prefix=old_path,
            new_prefix=department.path,
            exclude_id=department.id,
        )
    await session.commit()
    await session.refresh(department)
    return department


async def list_deleted_departments(
    session: AsyncSession,
) -> list[Department]:
    return await department_repository.list_deleted(session)


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
    "list_deleted_departments",
    "list_subtree_ids",
    "move_department",
    "restore_department",
    "update_department",
)
