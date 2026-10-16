"""角色业务逻辑。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.core.permissions import (
    invalidate_role_permissions,
    invalidate_super_admin_permissions,
)
from app.core.redis import redis_scope
from app.models.role import DataScope, Role, RoleStatus
from app.repositories import role as role_repository
from app.schemas.role import RoleCreate, RoleDataScopeUpdate, RoleUpdate
from app.services import menu as menu_service

_NON_NULLABLE_UPDATE_FIELDS = frozenset({"code", "name", "status"})
_SUPER_ADMIN_IMMUTABLE_UPDATE_FIELDS = frozenset({"code", "name", "status"})
# 种子初始化依赖这些代码；普通角色不得抢占。
_RESERVED_SYSTEM_ROLE_CODES = frozenset({"super_admin", "default"})


def _mysql_error_number(
    error: IntegrityError | OperationalError,
) -> int | None:
    args = getattr(error.orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _raise_mapped_integrity_error(error: IntegrityError) -> NoReturn:
    message = str(error.orig).lower()
    error_number = _mysql_error_number(error)
    if error_number == 1062 and (
        "role.ix_role_code" in message or "role.code" in message
    ):
        raise BusinessError(
            ErrorCode.CONFLICT,
            "Role code already exists",
            http_status=409,
        )
    if error_number == 1062:
        raise BusinessError(
            ErrorCode.CONFLICT,
            "角色授权正在被并发修改，请稍后重试",
            http_status=409,
        )
    if error_number == 1451 and "foreign key" in message and "`user`" in message:
        raise BusinessError(
            ErrorCode.ROLE_IN_USE,
            "Role is in use",
        )
    raise error


def _raise_mapped_operational_error(error: OperationalError) -> NoReturn:
    if _mysql_error_number(error) in {1205, 1213}:
        raise BusinessError(
            ErrorCode.CONFLICT,
            "角色授权正在被并发修改，请稍后重试",
            http_status=409,
        )
    raise error


@asynccontextmanager
async def _map_role_write_errors(session: AsyncSession) -> AsyncIterator[None]:
    try:
        yield
    except IntegrityError as error:
        await session.rollback()
        _raise_mapped_integrity_error(error)
    except OperationalError as error:
        await session.rollback()
        _raise_mapped_operational_error(error)


async def get_role(session: AsyncSession, role_id: int) -> Role:
    role = await role_repository.get_by_id(session, role_id)
    if role is None:
        raise BusinessError(
            ErrorCode.ROLE_NOT_FOUND,
            "Role not found",
            http_status=404,
        )
    return role


async def _invalidate_role_permission_cache(
    redis: Redis | None,
    role: Role,
) -> None:
    async with redis_scope(redis) as resolved_redis:
        await invalidate_role_permissions(resolved_redis, role.id)
        if role.code == "super_admin" and role.is_system:
            await invalidate_super_admin_permissions(resolved_redis)


async def _get_role_for_update(session: AsyncSession, role_id: int) -> Role:
    # 同一角色的整集合覆盖先按主键 FOR UPDATE 串行化；菜单/部门校验和
    # DELETE+INSERT 必须在同一事务内完成，避免并发 PUT 交叉替换。
    role = await role_repository.lock_by_id(session, role_id)
    if role is None:
        raise BusinessError(
            ErrorCode.ROLE_NOT_FOUND,
            "Role not found",
            http_status=404,
        )
    return role


def _ensure_role_code_is_not_reserved(
    code: str,
    *,
    is_system: bool,
    current_code: str | None,
) -> None:
    if code in _RESERVED_SYSTEM_ROLE_CODES and not (is_system and current_code == code):
        raise BusinessError(
            ErrorCode.CONFLICT,
            "Role code is reserved for a system role",
            http_status=409,
        )


async def _ensure_role_code_available(
    session: AsyncSession,
    code: str,
    *,
    exclude_id: int | None = None,
) -> None:
    existing = await role_repository.get_by_code(session, code)
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(
            ErrorCode.CONFLICT,
            "Role code already exists",
            http_status=409,
        )


async def create_role(session: AsyncSession, payload: RoleCreate) -> Role:
    async with _map_role_write_errors(session):
        _ensure_role_code_is_not_reserved(
            payload.code,
            is_system=False,
            current_code=None,
        )
        await _ensure_role_code_available(session, payload.code)
        role = Role(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            data_scope=payload.data_scope,
            data_scope_include_children=payload.data_scope_include_children,
            is_system=False,
            status=RoleStatus.ACTIVE,
        )
        await role_repository.add(session, role)
        await session.commit()
        await session.refresh(role)
        return role


async def list_roles(session: AsyncSession) -> list[Role]:
    return await role_repository.list_all(session)


async def update_role(
    session: AsyncSession,
    role_id: int,
    payload: RoleUpdate,
    *,
    redis: Redis | None = None,
) -> Role:
    async with _map_role_write_errors(session):
        role = await get_role(session, role_id)
        changes = payload.model_dump(exclude_unset=True)

        for field_name in _NON_NULLABLE_UPDATE_FIELDS:
            if field_name in changes and changes[field_name] is None:
                raise BusinessError(
                    ErrorCode.INVALID_PARAMETER,
                    f"{field_name} cannot be null",
                )

        if role.code == "super_admin" and role.is_system:
            if _SUPER_ADMIN_IMMUTABLE_UPDATE_FIELDS & changes.keys():
                raise BusinessError(
                    ErrorCode.SYSTEM_ROLE_IMMUTABLE,
                    "System role is immutable",
                )

        new_code = changes.get("code", role.code)
        _ensure_role_code_is_not_reserved(
            new_code,
            is_system=role.is_system,
            current_code=role.code,
        )
        await _ensure_role_code_available(
            session,
            new_code,
            exclude_id=role.id,
        )

        for field_name, value in changes.items():
            setattr(role, field_name, value)

        await session.commit()
        await session.refresh(role)
        await _invalidate_role_permission_cache(redis, role)
        return role


async def delete_role(
    session: AsyncSession,
    role_id: int,
    *,
    redis: Redis | None = None,
) -> None:
    async with _map_role_write_errors(session):
        role = await get_role(session, role_id)
        if role.is_system:
            raise BusinessError(
                ErrorCode.SYSTEM_ROLE_IMMUTABLE,
                "System role is immutable",
            )
        if await role_repository.has_users(session, role_id):
            raise BusinessError(
                ErrorCode.ROLE_IN_USE,
                "Role is in use",
            )

        await session.delete(role)
        await session.commit()
        await _invalidate_role_permission_cache(redis, role)


async def assign_role_menus(
    session: AsyncSession,
    role_id: int,
    menu_ids: list[int],
    *,
    redis: Redis | None = None,
) -> Role:
    async with _map_role_write_errors(session):
        role = await _get_role_for_update(session, role_id)
        unique_menu_ids = set(menu_ids)

        if role.code == "super_admin" and role.is_system:
            await role_repository.replace_role_menus(session, role.id, set())
            await session.commit()
            await _invalidate_role_permission_cache(redis, role)
            return role

        menus = await role_repository.list_menus_by_ids(session, unique_menu_ids)
        if {menu.id for menu in menus} != unique_menu_ids:
            raise BusinessError(
                ErrorCode.MENU_NOT_FOUND,
                "Menu not found",
                http_status=404,
            )

        await role_repository.replace_role_menus(
            session,
            role.id,
            unique_menu_ids,
        )
        await session.commit()
        await session.refresh(role)
        await _invalidate_role_permission_cache(redis, role)
        return role


async def set_role_data_scope(
    session: AsyncSession,
    role_id: int,
    payload: RoleDataScopeUpdate,
    *,
    redis: Redis | None = None,
) -> Role:
    async with _map_role_write_errors(session):
        role = await _get_role_for_update(session, role_id)

        if role.code == "super_admin" and role.is_system:
            if payload.data_scope != DataScope.ALL or payload.department_ids:
                raise BusinessError(
                    ErrorCode.SYSTEM_ROLE_IMMUTABLE,
                    "System role data scope is immutable",
                )
            role.data_scope = DataScope.ALL
            role.data_scope_include_children = True
            await role_repository.replace_role_departments(
                session,
                role.id,
                set(),
            )
            await session.commit()
            await session.refresh(role)
            await _invalidate_role_permission_cache(redis, role)
            return role

        unique_department_ids = set(payload.department_ids)
        if payload.data_scope == DataScope.CUSTOM:
            departments = await role_repository.list_departments_by_ids(
                session,
                unique_department_ids,
            )
            departments_by_id = {
                department.id: department for department in departments
            }
            if set(departments_by_id) != unique_department_ids:
                raise BusinessError(
                    ErrorCode.DEPT_NOT_FOUND,
                    "Department not found",
                    http_status=404,
                )
            if any(department.is_system for department in departments):
                raise BusinessError(
                    ErrorCode.INVALID_PARAMETER,
                    "System department cannot be used as a custom data scope",
                )

        role.data_scope = payload.data_scope
        role.data_scope_include_children = payload.data_scope_include_children
        await role_repository.replace_role_departments(
            session,
            role.id,
            unique_department_ids if payload.data_scope == DataScope.CUSTOM else set(),
        )
        await session.commit()
        await session.refresh(role)
        await _invalidate_role_permission_cache(redis, role)
        return role


async def list_role_permission_codes(
    session: AsyncSession,
    role: Role,
) -> frozenset[str]:
    if role.status == RoleStatus.DISABLED:
        return frozenset()
    if role.code == "super_admin" and role.is_system:
        return await menu_service.list_enabled_permission_codes(session)

    assigned_codes = await role_repository.list_assigned_permission_codes(
        session,
        role.id,
    )
    enabled_codes = await menu_service.list_enabled_permission_codes(session)
    return assigned_codes & enabled_codes


__all__: tuple[str, ...] = (
    "assign_role_menus",
    "create_role",
    "delete_role",
    "get_role",
    "list_role_permission_codes",
    "list_roles",
    "set_role_data_scope",
    "update_role",
)
