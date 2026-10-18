"""跟进方式、跟进记录与待跟进查询的业务逻辑。

本模块当前实现跟进方式的维护：列表、新增、改名、删除与恢复。跟进记录与
待跟进查询由后续任务追加到同一模块。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.follow_up import ContactFollowUpMethod
from app.repositories import follow_up as follow_up_repository
from app.services.audit import audited

_NAME_MAX_LENGTH = 64


def _forbidden_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FORBIDDEN,
        "Permission denied",
        http_status=403,
    )


def _method_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND,
        "Follow-up method not found",
        http_status=404,
    )


def _name_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS,
        "Follow-up method name already exists",
        http_status=409,
    )


def _method_in_use_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_METHOD_IN_USE,
        "Follow-up method is referenced by follow-up records",
        http_status=409,
    )


def _record_not_deleted_error() -> BusinessError:
    return BusinessError(
        ErrorCode.RECORD_NOT_DELETED,
        "Follow-up method is not deleted",
        http_status=409,
    )


def _invalid_name_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_INVALID_FIELD,
        message,
        http_status=422,
    )


def _require_super_admin(context: AuthContext) -> None:
    if not context.is_super_admin:
        raise _forbidden_error()


def _normalize_method_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise _invalid_name_error("Follow-up method name is required")
    if len(value) > _NAME_MAX_LENGTH:
        raise _invalid_name_error(
            "Follow-up method name must not exceed "
            f"{_NAME_MAX_LENGTH} characters"
        )
    return value


async def _assert_method_name_available(
    session: AsyncSession,
    name: str,
    *,
    exclude_method_id: int | None = None,
) -> None:
    """校验未删除方式中不存在同名，并用共享锁串行化并发写入。"""
    statement = (
        select(ContactFollowUpMethod.id)
        .where(
            ContactFollowUpMethod.status == ContactStatus.ACTIVE,
            ContactFollowUpMethod.name == name,
        )
        .limit(1)
        .with_for_update(read=True)
    )
    if exclude_method_id is not None:
        statement = statement.where(ContactFollowUpMethod.id != exclude_method_id)
    if await session.scalar(statement) is not None:
        raise _name_exists_error()


async def _flush_method(session: AsyncSession) -> None:
    """在保存点内刷写方式改动，把生成列唯一冲突转为名称重复错误。"""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as error:
        raise _name_exists_error() from error


async def _commit_method(
    session: AsyncSession,
    method: ContactFollowUpMethod,
) -> None:
    """在保存点内落库新方式，唯一键冲突转为名称重复错误。"""
    try:
        async with session.begin_nested():
            session.add(method)
            await session.flush()
    except IntegrityError as error:
        raise _name_exists_error() from error
    await session.refresh(method)
    await session.commit()


async def list_methods(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> list[ContactFollowUpMethod]:
    """未删除的跟进方式，按名称、ID 排序。

    方式字典供录入跟进记录的表单使用：接口层用 `customer:read` 把关，
    服务层不做超级管理员限制，`context` 仅为签名一致保留。
    """
    return await follow_up_repository.list_methods(
        session,
        status=ContactStatus.ACTIVE,
    )


async def list_deleted_methods(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> list[ContactFollowUpMethod]:
    """回收站：仅超级管理员可读的已删除跟进方式。"""
    _require_super_admin(context)
    return await follow_up_repository.list_methods(
        session,
        status=ContactStatus.DELETED,
    )


@audited(
    action="follow_up_method:create",
    resource_type="contact_follow_up_method",
    resource_id_attr="id",
)
async def create_method(
    session: AsyncSession,
    name: str,
    *,
    context: AuthContext,
) -> ContactFollowUpMethod:
    _require_super_admin(context)
    normalized = _normalize_method_name(name)
    await _assert_method_name_available(session, normalized)

    method = ContactFollowUpMethod(
        name=normalized,
        status=ContactStatus.ACTIVE,
        created_by=context.user_id,
        updated_by=context.user_id,
    )
    await _commit_method(session, method)
    return method


@audited(
    action="follow_up_method:update",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
async def update_method(
    session: AsyncSession,
    method_id: int,
    name: str,
    *,
    context: AuthContext,
) -> ContactFollowUpMethod:
    _require_super_admin(context)
    normalized = _normalize_method_name(name)

    method = await follow_up_repository.lock_method_for_update(session, method_id)
    if method is None or method.status != ContactStatus.ACTIVE:
        raise _method_not_found_error()

    await _assert_method_name_available(
        session,
        normalized,
        exclude_method_id=method.id,
    )
    method.name = normalized
    method.updated_by = context.user_id
    await _flush_method(session)
    await session.refresh(method)
    await session.commit()
    return method


@audited(
    action="follow_up_method:delete",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
async def delete_method(
    session: AsyncSession,
    method_id: int,
    *,
    context: AuthContext,
) -> None:
    _require_super_admin(context)

    method = await follow_up_repository.lock_method_for_update(session, method_id)
    if method is None or method.status != ContactStatus.ACTIVE:
        raise _method_not_found_error()

    # 当前读：引用检查必须包含已删除的跟进记录，且在软删除前完成。
    referenced_ids = await follow_up_repository.lock_follow_up_ids_referencing_method(
        session,
        method_id,
    )
    if referenced_ids:
        raise _method_in_use_error()

    method.status = ContactStatus.DELETED
    method.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    method.deleted_by = context.user_id
    method.updated_by = context.user_id
    await session.flush()
    await session.commit()


@audited(
    action="follow_up_method:restore",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
async def restore_method(
    session: AsyncSession,
    method_id: int,
    *,
    context: AuthContext,
) -> ContactFollowUpMethod:
    _require_super_admin(context)

    method = await follow_up_repository.lock_method_for_update(session, method_id)
    if method is None:
        raise _method_not_found_error()
    if method.status != ContactStatus.DELETED:
        raise _record_not_deleted_error()

    await _assert_method_name_available(
        session,
        method.name,
        exclude_method_id=method.id,
    )
    method.status = ContactStatus.ACTIVE
    method.deleted_at = None
    method.deleted_by = None
    method.updated_by = context.user_id
    await _flush_method(session)
    await session.refresh(method)
    await session.commit()
    return method


__all__: tuple[str, ...] = (
    "create_method",
    "delete_method",
    "list_deleted_methods",
    "list_methods",
    "restore_method",
    "update_method",
)