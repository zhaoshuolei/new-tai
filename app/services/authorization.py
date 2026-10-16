"""集中实现授权上限与防提权规则。"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope, Role, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import department as department_repository
from app.repositories import role as role_repository


def _forbidden(message: str) -> BusinessError:
    return BusinessError(ErrorCode.FORBIDDEN, message, http_status=403)


def _cannot_grant_super_admin() -> BusinessError:
    return BusinessError(
        ErrorCode.CANNOT_GRANT_SUPER_ADMIN,
        "Only a super administrator can grant or revoke super administrator",
        http_status=403,
    )


def _last_super_admin(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.LAST_SUPER_ADMIN,
        message,
        http_status=403,
    )


def is_system_super_admin(role: Role | None) -> bool:
    return role is not None and role.code == "super_admin" and role.is_system


def assignable_role_ids(
    ctx: AuthContext,
    all_roles: Iterable[Role],
) -> set[int]:
    """返回当前调用者可授予的启用角色集合。"""
    return {
        role.id
        for role in all_roles
        if role.status == RoleStatus.ACTIVE
        and (ctx.is_super_admin or not is_system_super_admin(role))
    }


def assert_can_assign_role(
    ctx: AuthContext,
    target_role: Role,
) -> None:
    """拒绝不可授予的角色，而不是静默忽略。"""
    if is_system_super_admin(target_role) and not ctx.is_super_admin:
        raise _cannot_grant_super_admin()
    if target_role.status != RoleStatus.ACTIVE:
        raise _forbidden("Disabled roles cannot be assigned")


def assert_not_self(
    ctx: AuthContext,
    target_user: User,
) -> None:
    if ctx.user_id == target_user.id:
        raise BusinessError(
            ErrorCode.CANNOT_MODIFY_SELF,
            "Cannot modify your own role, department, or status",
            http_status=403,
        )


def assert_target_visible(
    ctx: AuthContext,
    target_user: User,
) -> None:
    """对已加载的目标做数据范围判断。

    用户写入路径仍先使用带数据范围过滤的查询读取目标；此函数为不依赖
    数据库路径信息的上下文提供集中校验。
    """
    if ctx.is_super_admin or ctx.data_scope == DataScope.ALL:
        return
    if ctx.user_id == target_user.id:
        return
    if ctx.data_scope == DataScope.DEPT:
        if (
            ctx.department_id is not None
            and target_user.department_id == ctx.department_id
        ):
            return
        raise _forbidden("User is outside the current data scope")
    if ctx.data_scope == DataScope.DEPT_AND_CHILD:
        if (
            ctx.department_id is not None
            and target_user.department_id == ctx.department_id
        ):
            return
        raise _forbidden("User is outside the current data scope")
    if ctx.data_scope == DataScope.CUSTOM:
        if target_user.department_id in ctx.custom_department_ids:
            return
        raise _forbidden("User is outside the current data scope")
    raise _forbidden("User is outside the current data scope")


async def assert_can_assign_department(
    session: AsyncSession,
    ctx: AuthContext,
    target_department: Department,
) -> None:
    """拒绝停用部门及超出当前数据范围的部门。"""
    if target_department.status != DepartmentStatus.ACTIVE:
        raise _forbidden("Disabled departments cannot be assigned")

    if ctx.is_super_admin or ctx.data_scope == DataScope.ALL:
        return

    if target_department.is_system:
        raise _forbidden("System departments cannot be assigned by this caller")

    if ctx.data_scope == DataScope.SELF or ctx.department_id is None:
        raise _forbidden("Department is outside the caller's grant scope")

    current_department = await department_repository.get_by_id(
        session,
        ctx.department_id,
    )
    if (
        current_department is None
        or current_department.is_system
        or current_department.status != DepartmentStatus.ACTIVE
    ):
        raise _forbidden("Caller has no valid department grant scope")

    if ctx.data_scope == DataScope.DEPT:
        if target_department.id != current_department.id:
            raise _forbidden("Department is outside the caller's grant scope")
        return

    if ctx.data_scope == DataScope.DEPT_AND_CHILD:
        if target_department.path.startswith(current_department.path):
            return
        raise _forbidden("Department is outside the caller's grant scope")

    if ctx.data_scope == DataScope.CUSTOM:
        if not ctx.include_children:
            if target_department.id in ctx.custom_department_ids:
                return
            raise _forbidden("Department is outside the caller's grant scope")

        roots = await role_repository.list_departments_by_ids(
            session,
            set(ctx.custom_department_ids),
        )
        if any(
            root.status == DepartmentStatus.ACTIVE
            and not root.is_system
            and target_department.path.startswith(root.path)
            for root in roots
        ):
            return
        raise _forbidden("Department is outside the caller's grant scope")

    raise _forbidden("Department is outside the caller's grant scope")


async def assert_super_admin_remains(
    session: AsyncSession,
    operation: str,
) -> None:
    """锁定可用超管集合，保证扣减后仍至少保留一个。"""
    statement = (
        select(User.id)
        .join(Role, User.role_id == Role.id)
        .where(
            Role.code == "super_admin",
            Role.is_system.is_(True),
            Role.status == RoleStatus.ACTIVE,
            User.status == UserStatus.ACTIVE,
        )
        .with_for_update()
    )
    result = await session.scalars(statement)
    usable_super_admins = list(result.all())
    if len(usable_super_admins) <= 1:
        raise _last_super_admin(
            f"Cannot {operation} the last usable super administrator"
        )


async def assert_can_reduce_super_admin(
    session: AsyncSession,
    target_user: User,
    current_role: Role | None,
    operation: str,
) -> None:
    """保护本地应急超管，并阻止最后一名可用超管被降权。"""
    if not is_system_super_admin(current_role):
        return
    if target_user.feishu_open_id is None:
        raise _last_super_admin(
            "The local emergency super administrator must be preserved"
        )
    if target_user.status != UserStatus.ACTIVE:
        return
    await assert_super_admin_remains(session, operation)


def assert_can_manage_super_admin_role(
    ctx: AuthContext,
    role: Role,
) -> None:
    if is_system_super_admin(role) and not ctx.is_super_admin:
        raise _cannot_grant_super_admin()


__all__: tuple[str, ...] = (
    "assert_can_assign_department",
    "assert_can_assign_role",
    "assert_can_manage_super_admin_role",
    "assert_can_reduce_super_admin",
    "assert_not_self",
    "assert_super_admin_remains",
    "assert_target_visible",
    "assignable_role_ids",
    "is_system_super_admin",
)
