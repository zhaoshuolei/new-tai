"""跟进方式、跟进记录与待跟进查询的业务逻辑。

本模块实现跟进方式的维护，以及跟进记录的查询、新增、部分更新、删除、
恢复和今日/逾期待跟进查询。
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.datetime_utils import business_day_bounds_utc, to_utc_naive
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus, CustomerContact
from app.models.customer import Customer
from app.models.follow_up import ContactFollowUp, ContactFollowUpMethod
from app.models.user import User, UserStatus
from app.repositories import contact as contact_repository
from app.repositories import follow_up as follow_up_repository
from app.repositories.follow_up import DueCustomerRow, DueFollowUpRow, FollowUpRow
from app.schemas.follow_up import FollowUpCreate, FollowUpUpdate
from app.services.audit import audited
from app.services.data_scope import (
    apply_customer_scope,
    apply_user_scope,
    build_customer_scope_filter,
)

_NAME_MAX_LENGTH = 64
_CONTENT_MAX_LENGTH = 5000
_FUTURE_GRACE = timedelta(minutes=5)


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


# 跟进记录：错误构造、公开守卫、私有校验与 CRUD。


def _customer_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_NOT_FOUND,
        "Customer not found",
        http_status=404,
    )


def _contact_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_CONTACT_NOT_FOUND,
        "Customer contact not found",
        http_status=404,
    )


def _follow_up_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_NOT_FOUND,
        "Follow-up record not found",
        http_status=404,
    )


def _follow_up_not_owner_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_NOT_OWNER,
        "Only the current follow-up user may modify this record",
        http_status=403,
    )


def _follow_up_user_unavailable_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_USER_UNAVAILABLE,
        "Follow-up user is unavailable",
        http_status=422,
    )


def _follow_up_not_deleted_error() -> BusinessError:
    return BusinessError(
        ErrorCode.RECORD_NOT_DELETED,
        "Follow-up record is not deleted",
        http_status=409,
    )


def _invalid_follow_up_field_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_INVALID_FIELD,
        message,
        http_status=422,
    )


def _now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def require_visible_active_customer(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
) -> None:
    """要求客户存在、未删除且在当前用户的数据范围内。"""
    statement = apply_customer_scope(
        select(Customer.id).where(
            Customer.id == customer_id,
            Customer.status == ContactStatus.ACTIVE,
        ),
        context,
    )
    if await session.scalar(statement) is None:
        raise _customer_not_found_error()


async def require_active_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
) -> CustomerContact:
    """要求联系人属于指定客户且未删除。"""
    contact = await contact_repository.get_contact(session, customer_id, contact_id)
    if contact is None or contact.status != ContactStatus.ACTIVE:
        raise _contact_not_found_error()
    return contact


async def require_active_follow_up(
    session: AsyncSession,
    contact_id: int,
    follow_up_id: int,
) -> ContactFollowUp:
    """要求跟进记录属于指定联系人且未删除。"""
    follow_up = await follow_up_repository.get_follow_up(
        session,
        contact_id,
        follow_up_id,
    )
    if follow_up is None or follow_up.status != ContactStatus.ACTIVE:
        raise _follow_up_not_found_error()
    return follow_up


def require_follow_up_owner(
    record: ContactFollowUp,
    *,
    context: AuthContext,
) -> None:
    """要求调用者是记录当前跟进人；超级管理员也不绕过此规则。"""
    if record.follow_up_user_id != context.user_id:
        raise _follow_up_not_owner_error()


async def _resolve_follow_up_user(
    session: AsyncSession,
    user_id: object,
    *,
    context: AuthContext,
) -> int:
    if not isinstance(user_id, int):
        raise _follow_up_user_unavailable_error()
    statement = apply_user_scope(
        select(User.id).where(
            User.id == user_id,
            User.deleted_at.is_(None),
            User.status == UserStatus.ACTIVE,
        ),
        context,
    )
    resolved = await session.scalar(statement)
    if not isinstance(resolved, int):
        raise _follow_up_user_unavailable_error()
    return resolved


async def _require_active_method(
    session: AsyncSession,
    method_id: int,
) -> ContactFollowUpMethod:
    method = await follow_up_repository.lock_method_shared(session, method_id)
    if method is None or method.status != ContactStatus.ACTIVE:
        raise _method_not_found_error()
    return method


def _normalize_follow_up_content(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_follow_up_field_error("Follow-up content is required")
    content = value.strip()
    if not content:
        raise _invalid_follow_up_field_error("Follow-up content is required")
    if len(content) > _CONTENT_MAX_LENGTH:
        raise _invalid_follow_up_field_error(
            f"Follow-up content must not exceed {_CONTENT_MAX_LENGTH} characters"
        )
    return content


def _normalize_follow_up_datetime(
    value: object,
    *,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise _invalid_follow_up_field_error(f"{field_name} is invalid")
    normalized = to_utc_naive(value)
    assert normalized is not None
    return normalized


def _validate_follow_up_fields(
    changes: dict[str, object],
    *,
    current_follow_up_at: datetime | None = None,
    current_next_follow_up_at: datetime | None = None,
) -> None:
    """归一化并校验内容与时间，使用记录现值补全未更新字段。"""
    if "content" in changes:
        changes["content"] = _normalize_follow_up_content(changes["content"])

    follow_up_at = _normalize_follow_up_datetime(
        changes.get("follow_up_at", current_follow_up_at),
        field_name="follow_up_at",
    )
    if follow_up_at is None:
        raise _invalid_follow_up_field_error("Follow-up time is required")
    if "follow_up_at" in changes:
        changes["follow_up_at"] = follow_up_at
        if follow_up_at > _now_naive() + _FUTURE_GRACE:
            raise _invalid_follow_up_field_error(
                "Follow-up time must not be later than five minutes from now"
            )

    next_follow_up_at = _normalize_follow_up_datetime(
        changes.get("next_follow_up_at", current_next_follow_up_at),
        field_name="next_follow_up_at",
    )
    if "next_follow_up_at" in changes:
        changes["next_follow_up_at"] = next_follow_up_at
    if next_follow_up_at is not None and next_follow_up_at <= follow_up_at:
        raise _invalid_follow_up_field_error(
            "Next follow-up time must be later than follow-up time"
        )


async def _follow_up_row(
    session: AsyncSession,
    follow_up_id: int,
) -> FollowUpRow:
    row = await follow_up_repository.get_follow_up_row(session, follow_up_id)
    if row is None:
        raise _follow_up_not_found_error()
    return row


async def list_follow_ups(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[FollowUpRow], int]:
    """列出客户联系人未删除的跟进记录。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)
    return await follow_up_repository.list_follow_ups(
        session,
        contact_id,
        status=ContactStatus.ACTIVE,
        follow_up_user_id=None,
        page=page,
        page_size=page_size,
    )


async def list_deleted_follow_ups(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[FollowUpRow], int]:
    """回收站只展示当前用户作为跟进人的已删除记录。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)
    return await follow_up_repository.list_follow_ups(
        session,
        contact_id,
        status=ContactStatus.DELETED,
        follow_up_user_id=context.user_id,
        page=page,
        page_size=page_size,
    )


@audited(
    action="contact_follow_up:create",
    resource_type="contact_follow_up",
    resource_id_attr="id",
)
async def create_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    payload: FollowUpCreate,
    *,
    context: AuthContext,
) -> FollowUpRow:
    """新增跟进记录，默认由调用者跟进，录入人始终为调用者。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)

    changes: dict[str, object] = payload.model_dump()
    method_id = changes.get("method_id")
    if not isinstance(method_id, int):
        raise _invalid_follow_up_field_error("Follow-up method is required")
    await _require_active_method(session, method_id)

    follow_up_user_id = changes.get("follow_up_user_id")
    if follow_up_user_id is None:
        follow_up_user_id = context.user_id
    changes["follow_up_user_id"] = await _resolve_follow_up_user(
        session,
        follow_up_user_id,
        context=context,
    )

    if changes.get("follow_up_at") is None:
        changes["follow_up_at"] = _now_naive()
    _validate_follow_up_fields(changes)

    follow_up = ContactFollowUp(
        contact_id=contact_id,
        status=ContactStatus.ACTIVE,
        created_by=context.user_id,
        updated_by=context.user_id,
        **changes,
    )
    await follow_up_repository.add_follow_up(session, follow_up)
    await session.commit()
    return await _follow_up_row(session, follow_up.id)


@audited(
    action="contact_follow_up:update",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
async def update_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    payload: FollowUpUpdate,
    *,
    context: AuthContext,
) -> FollowUpRow:
    """部分更新记录；当前跟进人校验发生在任何字段写入之前。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)

    initial = await require_active_follow_up(session, contact_id, follow_up_id)
    require_follow_up_owner(initial, context=context)
    changes: dict[str, object] = payload.model_dump(exclude_unset=True)

    method_id = changes.get("method_id", initial.method_id)
    if not isinstance(method_id, int):
        raise _invalid_follow_up_field_error("Follow-up method is required")
    await _require_active_method(session, method_id)

    follow_up = await follow_up_repository.lock_follow_up_for_update(
        session,
        contact_id,
        follow_up_id,
    )
    if follow_up is None or follow_up.status != ContactStatus.ACTIVE:
        raise _follow_up_not_found_error()
    require_follow_up_owner(follow_up, context=context)

    if "follow_up_user_id" in changes:
        changes["follow_up_user_id"] = await _resolve_follow_up_user(
            session,
            changes["follow_up_user_id"],
            context=context,
        )

    _validate_follow_up_fields(
        changes,
        current_follow_up_at=follow_up.follow_up_at,
        current_next_follow_up_at=follow_up.next_follow_up_at,
    )
    for field_name, value in changes.items():
        setattr(follow_up, field_name, value)
    follow_up.updated_by = context.user_id

    await session.flush()
    await session.commit()
    return await _follow_up_row(session, follow_up.id)


@audited(
    action="contact_follow_up:delete",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
async def delete_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    *,
    context: AuthContext,
) -> None:
    """仅当前跟进人可软删除自己的记录。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)

    follow_up = await follow_up_repository.lock_follow_up_for_update(
        session,
        contact_id,
        follow_up_id,
    )
    if follow_up is None or follow_up.status != ContactStatus.ACTIVE:
        raise _follow_up_not_found_error()
    require_follow_up_owner(follow_up, context=context)

    follow_up.status = ContactStatus.DELETED
    follow_up.deleted_at = _now_naive()
    follow_up.deleted_by = context.user_id
    follow_up.updated_by = context.user_id
    await session.flush()
    await session.commit()


@audited(
    action="contact_follow_up:restore",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
async def restore_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    *,
    context: AuthContext,
) -> FollowUpRow:
    """仅当前跟进人可恢复自己的已删除记录。"""
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)

    follow_up = await follow_up_repository.lock_follow_up_for_update(
        session,
        contact_id,
        follow_up_id,
    )
    if follow_up is None:
        raise _follow_up_not_found_error()
    if follow_up.status != ContactStatus.DELETED:
        raise _follow_up_not_deleted_error()
    require_follow_up_owner(follow_up, context=context)

    follow_up.status = ContactStatus.ACTIVE
    follow_up.deleted_at = None
    follow_up.deleted_by = None
    follow_up.updated_by = context.user_id
    await session.flush()
    await session.commit()
    return await _follow_up_row(session, follow_up.id)


# 待跟进查询：业务日边界、可见范围与分页。


async def list_due_follow_ups(
    session: AsyncSession,
    *,
    context: AuthContext,
    due: Literal["today", "overdue", "all"],
    follow_up_user_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[DueFollowUpRow], int]:
    """分页列出当前用户数据范围内今日或逾期待跟进的对接人。"""
    today_start, tomorrow_start = business_day_bounds_utc(_now_naive())
    return await follow_up_repository.list_due_follow_ups(
        session,
        scope_filter=build_customer_scope_filter(context),
        due_state=due,
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
        page=page,
        page_size=page_size,
    )


async def list_due_customers(
    session: AsyncSession,
    *,
    context: AuthContext,
    due: Literal["today", "overdue", "all"],
    follow_up_user_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[DueCustomerRow], int]:
    """分页列出客户维度的待跟进对接人数与最早计划时间。"""
    today_start, tomorrow_start = business_day_bounds_utc(_now_naive())
    return await follow_up_repository.list_due_customers(
        session,
        scope_filter=build_customer_scope_filter(context),
        due_state=due,
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
        page=page,
        page_size=page_size,
    )


async def due_summary(
    session: AsyncSession,
    *,
    context: AuthContext,
    follow_up_user_id: int | None,
) -> tuple[int, int]:
    """统计今日与逾期待跟进对接人数。"""
    today_start, tomorrow_start = business_day_bounds_utc(_now_naive())
    return await follow_up_repository.count_due_states(
        session,
        scope_filter=build_customer_scope_filter(context),
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
    )


__all__: tuple[str, ...] = (
    "create_follow_up",
    "create_method",
    "delete_follow_up",
    "delete_method",
    "due_summary",
    "list_deleted_follow_ups",
    "list_deleted_methods",
    "list_due_customers",
    "list_due_follow_ups",
    "list_follow_ups",
    "list_methods",
    "require_active_contact",
    "require_active_follow_up",
    "require_follow_up_owner",
    "require_visible_active_customer",
    "restore_follow_up",
    "restore_method",
    "update_follow_up",
    "update_method",
)
