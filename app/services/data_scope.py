"""用户资源的数据范围过滤。"""

from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    false,
    func,
    or_,
    select,
    true,
)
from sqlalchemy.orm import aliased

from app.core.auth_context import AuthContext
from app.models.department import Department
from app.models.role import DataScope
from app.models.user import User


def _current_department_is_business(department_id: int) -> ColumnElement[bool]:
    return (
        select(Department.id)
        .where(
            Department.id == department_id,
            Department.is_system.is_(False),
        )
        .exists()
    )


def _current_department_path(department_id: int) -> Any:
    return (
        select(Department.path)
        .where(
            Department.id == department_id,
            Department.is_system.is_(False),
        )
        .scalar_subquery()
    )


def _custom_subtree_condition(
    custom_department_ids: frozenset[int],
) -> ColumnElement[bool]:
    if not custom_department_ids:
        return false()

    target_department = aliased(Department)
    subtree_conditions: list[ColumnElement[bool]] = []
    for department_id in custom_department_ids:
        path = (
            select(Department.path)
            .where(
                Department.id == department_id,
                Department.is_system.is_(False),
            )
            .scalar_subquery()
        )
        subtree_conditions.append(target_department.path.like(func.concat(path, "%")))

    return User.department_id.in_(
        select(target_department.id).where(or_(*subtree_conditions))
    )


def build_user_scope_filter(ctx: AuthContext) -> ColumnElement[bool]:
    if ctx.role_id is None or ctx.data_scope is None:
        return User.id == ctx.user_id

    if ctx.is_super_admin or ctx.data_scope == DataScope.ALL:
        return true()

    if ctx.data_scope == DataScope.SELF:
        return User.id == ctx.user_id

    if ctx.department_id is None:
        return false()

    current_department_is_business = _current_department_is_business(ctx.department_id)
    if ctx.data_scope == DataScope.DEPT:
        return and_(
            current_department_is_business,
            User.department_id == ctx.department_id,
        )

    if ctx.data_scope == DataScope.DEPT_AND_CHILD:
        target_department = aliased(Department)
        current_path = _current_department_path(ctx.department_id)
        return and_(
            current_department_is_business,
            User.department_id.in_(
                select(target_department.id).where(
                    target_department.path.like(func.concat(current_path, "%"))
                )
            ),
        )

    if ctx.data_scope == DataScope.CUSTOM:
        if ctx.include_children:
            condition = _custom_subtree_condition(ctx.custom_department_ids)
        elif not ctx.custom_department_ids:
            condition = false()
        else:
            condition = User.department_id.in_(
                select(Department.id).where(
                    Department.id.in_(ctx.custom_department_ids),
                    Department.is_system.is_(False),
                )
            )
        return and_(current_department_is_business, condition)

    return false()


def apply_user_scope(
    stmt: Select[Any],
    ctx: AuthContext,
) -> Select[Any]:
    return stmt.where(build_user_scope_filter(ctx))


__all__: tuple[str, ...] = ("apply_user_scope", "build_user_scope_filter")
