"""联系人职能业务逻辑：共用/自定义维护、删除迁移与恢复。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import (
    ContactFunction,
    ContactFunctionNameLock,
    ContactStatus,
    CustomerContact,
)
from app.models.customer import Customer
from app.repositories import contact as contact_repository
from app.schemas.contact import ContactCreate, ContactUpdate
from app.services.audit import audited
from app.services.contact_validation import normalize_email, normalize_phone
from app.services.data_scope import apply_customer_scope

FunctionScope = Literal["SHARED", "CUSTOM"]


def _forbidden_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FORBIDDEN,
        "Permission denied",
        http_status=403,
    )


def _customer_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_NOT_FOUND,
        "Customer not found",
        http_status=404,
    )


def _function_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_NOT_FOUND,
        "Contact function not found",
        http_status=404,
    )


def _name_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_NAME_EXISTS,
        "Contact function name already exists",
        http_status=409,
    )


def _replacement_required_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED,
        "A replacement function is required",
        http_status=422,
    )


def _replacement_invalid_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID,
        "Replacement function is invalid",
        http_status=422,
    )


def _record_not_deleted_error() -> BusinessError:
    return BusinessError(
        ErrorCode.RECORD_NOT_DELETED,
        "Contact function is not deleted",
        http_status=409,
    )


def _invalid_name_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_INVALID_FIELD,
        message,
        http_status=422,
    )


def _contact_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_CONTACT_NOT_FOUND,
        "Customer contact not found",
        http_status=404,
    )


def _contact_function_unavailable_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_UNAVAILABLE,
        "Contact function is unavailable for this customer",
        http_status=422,
    )


def _contact_not_deleted_error() -> BusinessError:
    return BusinessError(
        ErrorCode.RECORD_NOT_DELETED,
        "Customer contact is not deleted",
        http_status=409,
    )


def _invalid_contact_field_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_INVALID_FIELD,
        message,
        http_status=422,
    )


def _normalize_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise _invalid_name_error("Contact function name is required")
    if len(value) > 64:
        raise _invalid_name_error("Contact function name must not exceed 64 characters")
    return value


def _require_super_admin(context: AuthContext) -> None:
    if not context.is_super_admin:
        raise _forbidden_error()


@asynccontextmanager
async def _serialize_name_change(session: AsyncSession) -> AsyncIterator[None]:
    lock_id = await session.scalar(
        select(ContactFunctionNameLock.id)
        .where(ContactFunctionNameLock.id == 1)
        .with_for_update()
    )
    if lock_id is None:
        raise RuntimeError(
            "contact_function_name_lock singleton row id=1 is missing; "
            "apply the database migration"
        )

    try:
        yield
    except BaseException:
        await session.rollback()
        raise


async def _require_visible_active_customer(
    session: AsyncSession,
    customer_id: int,
    context: AuthContext,
) -> None:
    statement = apply_customer_scope(
        select(Customer.id).where(
            Customer.id == customer_id,
            Customer.status == ContactStatus.ACTIVE,
        ),
        context,
    )
    if await session.scalar(statement) is None:
        raise _customer_not_found_error()


def _normalize_optional_contact_text(
    value: object,
    *,
    field_name: str,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_contact_field_error(f"Contact {field_name} is invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if max_length is not None and len(normalized) > max_length:
        raise _invalid_contact_field_error(
            f"Contact {field_name} must not exceed {max_length} characters"
        )
    return normalized


def _validate_contact_fields(changes: dict[str, object]) -> dict[str, object]:
    if "name" in changes:
        value = changes["name"]
        if not isinstance(value, str):
            raise _invalid_contact_field_error("Contact name is required")
        name = value.strip()
        if not name:
            raise _invalid_contact_field_error("Contact name is required")
        if len(name) > 255:
            raise _invalid_contact_field_error(
                "Contact name must not exceed 255 characters"
            )
        changes["name"] = name

    for field_name, max_length in (
        ("position", 128),
        ("home_address", 512),
        ("remark", None),
    ):
        if field_name in changes:
            changes[field_name] = _normalize_optional_contact_text(
                changes[field_name],
                field_name=field_name,
                max_length=max_length,
            )

    if "phone" in changes:
        phone = _normalize_optional_contact_text(
            changes["phone"],
            field_name="phone",
        )
        try:
            changes["phone"] = normalize_phone(phone)
        except ValueError as error:
            raise _invalid_contact_field_error("Contact phone is invalid") from error

    if "email" in changes:
        email = _normalize_optional_contact_text(
            changes["email"],
            field_name="email",
        )
        try:
            changes["email"] = normalize_email(email)
        except ValueError as error:
            raise _invalid_contact_field_error("Contact email is invalid") from error

    return changes


async def _assert_contact_function_usable(
    session: AsyncSession,
    customer_id: int,
    function_id: int | None,
) -> None:
    if function_id is None:
        raise _contact_function_unavailable_error()

    function = await contact_repository.get_function(session, function_id)
    if (
        function is None
        or function.status != ContactStatus.ACTIVE
        or (function.customer_id is not None and function.customer_id != customer_id)
    ):
        raise _contact_function_unavailable_error()

    locked = await contact_repository.lock_function_shared(session, function_id)
    if (
        locked is None
        or locked.status != ContactStatus.ACTIVE
        or (locked.customer_id is not None and locked.customer_id != customer_id)
    ):
        raise _contact_function_unavailable_error()


async def _require_active_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
) -> CustomerContact:
    contact = await contact_repository.get_contact(session, customer_id, contact_id)
    if contact is None or contact.status != ContactStatus.ACTIVE:
        raise _contact_not_found_error()
    return contact


async def _assert_name_available(
    session: AsyncSession,
    name: str,
    *,
    customer_id: int | None,
    exclude_function_id: int | None = None,
) -> None:
    conditions = [
        ContactFunction.status == ContactStatus.ACTIVE,
        ContactFunction.name == name,
    ]
    if customer_id is not None:
        conditions.append(
            or_(
                ContactFunction.customer_id.is_(None),
                ContactFunction.customer_id == customer_id,
            )
        )
    if exclude_function_id is not None:
        conditions.append(ContactFunction.id != exclude_function_id)

    statement = (
        select(ContactFunction.id)
        .where(*conditions)
        .limit(1)
        .with_for_update(read=True)
    )
    if await session.scalar(statement) is not None:
        raise _name_exists_error()


async def _commit_create(
    session: AsyncSession,
    function: ContactFunction,
) -> None:
    try:
        async with session.begin_nested():
            session.add(function)
            await session.flush()
    except IntegrityError as error:
        raise _name_exists_error() from error
    await session.refresh(function)
    await session.commit()


async def _validate_replacement(
    target: ContactFunction,
    replacement: ContactFunction | None,
    contact_count: int,
    *,
    replacement_requested: bool,
) -> ContactFunction | None:
    if not replacement_requested:
        if contact_count > 0:
            raise _replacement_required_error()
        return None

    if (
        replacement is None
        or contact_count == 0
        or replacement.status != ContactStatus.ACTIVE
        or replacement.id == target.id
    ):
        raise _replacement_invalid_error()
    if target.customer_id is None:
        if replacement.customer_id is not None:
            raise _replacement_invalid_error()
    elif replacement.customer_id not in (None, target.customer_id):
        raise _replacement_invalid_error()
    return replacement


async def _delete_function(
    session: AsyncSession,
    function_id: int,
    *,
    replacement_function_id: int | None,
    customer_id: int | None,
    context: AuthContext,
) -> int:
    target = await contact_repository.lock_function_for_update(session, function_id)
    if target is None or target.status != ContactStatus.ACTIVE:
        raise _function_not_found_error()
    if customer_id is not None and target.customer_id != customer_id:
        raise _function_not_found_error()

    replacement_requested = replacement_function_id is not None
    replacement = None
    if replacement_requested:
        replacement = await contact_repository.lock_function_shared(
            session,
            replacement_function_id,
        )

    contact_count = await contact_repository.count_contacts_referencing(
        session,
        function_id,
    )
    replacement = await _validate_replacement(
        target,
        replacement,
        contact_count,
        replacement_requested=replacement_requested,
    )
    if replacement is not None:
        await contact_repository.reassign_contacts(
            session,
            function_id,
            replacement.id,
        )

    target.status = ContactStatus.DELETED
    target.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    target.deleted_by = context.user_id
    target.updated_by = context.user_id
    await session.commit()
    return contact_count


async def list_shared_functions(
    session: AsyncSession,
    *,
    context: AuthContext,
    include_deleted: bool = False,
) -> list[ContactFunction]:
    if not include_deleted:
        return await contact_repository.list_shared_functions(
            session,
            status=ContactStatus.ACTIVE,
        )

    _require_super_admin(context)
    active = await contact_repository.list_shared_functions(
        session,
        status=ContactStatus.ACTIVE,
    )
    deleted = await contact_repository.list_shared_functions(
        session,
        status=ContactStatus.DELETED,
    )
    return sorted([*active, *deleted], key=lambda row: (row.name, row.id))


@audited(
    action="contact_function:create",
    resource_type="contact_function",
    resource_id_attr="id",
)
async def create_shared_function(
    session: AsyncSession,
    name: str,
    *,
    context: AuthContext,
) -> ContactFunction:
    _require_super_admin(context)
    normalized = _normalize_name(name)
    async with _serialize_name_change(session):
        await _assert_name_available(
            session,
            normalized,
            customer_id=None,
        )
        function = ContactFunction(
            name=normalized,
            customer_id=None,
            scope_key=0,
            status=ContactStatus.ACTIVE,
            created_by=context.user_id,
            updated_by=context.user_id,
        )
        await _commit_create(session, function)
    return function


@audited(
    action="contact_function:update",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def update_shared_function(
    session: AsyncSession,
    function_id: int,
    name: str,
    *,
    context: AuthContext,
) -> ContactFunction:
    _require_super_admin(context)
    normalized = _normalize_name(name)
    async with _serialize_name_change(session):
        function = await contact_repository.get_function(session, function_id)
        if (
            function is None
            or function.customer_id is not None
            or function.status != ContactStatus.ACTIVE
        ):
            raise _function_not_found_error()

        await _assert_name_available(
            session,
            normalized,
            customer_id=None,
            exclude_function_id=function.id,
        )
        try:
            async with session.begin_nested():
                function.name = normalized
                function.updated_by = context.user_id
                await session.flush()
        except IntegrityError as error:
            raise _name_exists_error() from error
        await session.refresh(function)
        await session.commit()
    return function


@audited(
    action="contact_function:delete",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def delete_shared_function(
    session: AsyncSession,
    function_id: int,
    *,
    replacement_function_id: int | None,
    context: AuthContext,
) -> int:
    _require_super_admin(context)
    return await _delete_function(
        session,
        function_id,
        replacement_function_id=replacement_function_id,
        customer_id=None,
        context=context,
    )


@audited(
    action="contact_function:restore",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def restore_shared_function(
    session: AsyncSession,
    function_id: int,
    *,
    context: AuthContext,
) -> ContactFunction:
    _require_super_admin(context)
    async with _serialize_name_change(session):
        function = await contact_repository.get_function(session, function_id)
        if function is None or function.customer_id is not None:
            raise _function_not_found_error()
        if function.status != ContactStatus.DELETED:
            raise _record_not_deleted_error()

        await _assert_name_available(
            session,
            function.name,
            customer_id=None,
            exclude_function_id=function.id,
        )
        function.status = ContactStatus.ACTIVE
        function.deleted_at = None
        function.deleted_by = None
        function.updated_by = context.user_id
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as error:
            raise _name_exists_error() from error
        await session.refresh(function)
        await session.commit()
    return function


async def list_customer_functions(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
) -> list[tuple[ContactFunction, FunctionScope]]:
    await _require_visible_active_customer(session, customer_id, context)
    functions = await contact_repository.list_functions_for_customer(
        session,
        customer_id,
    )
    return [
        (
            function,
            "SHARED" if function.customer_id is None else "CUSTOM",
        )
        for function in functions
    ]


@audited(
    action="contact_function:create",
    resource_type="contact_function",
    resource_id_attr="id",
)
async def create_custom_function(
    session: AsyncSession,
    customer_id: int,
    name: str,
    *,
    context: AuthContext,
) -> ContactFunction:
    normalized = _normalize_name(name)
    async with _serialize_name_change(session):
        await _require_visible_active_customer(session, customer_id, context)
        await _assert_name_available(
            session,
            normalized,
            customer_id=customer_id,
        )
        function = ContactFunction(
            name=normalized,
            customer_id=customer_id,
            scope_key=customer_id,
            status=ContactStatus.ACTIVE,
            created_by=context.user_id,
            updated_by=context.user_id,
        )
        await _commit_create(session, function)
    return function


@audited(
    action="contact_function:update",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def update_custom_function(
    session: AsyncSession,
    customer_id: int,
    function_id: int,
    name: str,
    *,
    context: AuthContext,
) -> ContactFunction:
    normalized = _normalize_name(name)
    async with _serialize_name_change(session):
        await _require_visible_active_customer(session, customer_id, context)
        function = await contact_repository.get_function(session, function_id)
        if (
            function is None
            or function.customer_id != customer_id
            or function.status != ContactStatus.ACTIVE
        ):
            raise _function_not_found_error()

        await _assert_name_available(
            session,
            normalized,
            customer_id=customer_id,
            exclude_function_id=function.id,
        )
        try:
            async with session.begin_nested():
                function.name = normalized
                function.updated_by = context.user_id
                await session.flush()
        except IntegrityError as error:
            raise _name_exists_error() from error
        await session.refresh(function)
        await session.commit()
    return function


@audited(
    action="contact_function:delete",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def delete_custom_function(
    session: AsyncSession,
    customer_id: int,
    function_id: int,
    *,
    replacement_function_id: int | None,
    context: AuthContext,
) -> int:
    await _require_visible_active_customer(session, customer_id, context)
    return await _delete_function(
        session,
        function_id,
        replacement_function_id=replacement_function_id,
        customer_id=customer_id,
        context=context,
    )


@audited(
    action="contact_function:restore",
    resource_type="contact_function",
    resource_id_param="function_id",
)
async def restore_custom_function(
    session: AsyncSession,
    customer_id: int,
    function_id: int,
    *,
    context: AuthContext,
) -> ContactFunction:
    async with _serialize_name_change(session):
        await _require_visible_active_customer(session, customer_id, context)
        function = await contact_repository.get_function(session, function_id)
        if function is None or function.customer_id != customer_id:
            raise _function_not_found_error()
        if function.status != ContactStatus.DELETED:
            raise _record_not_deleted_error()

        await _assert_name_available(
            session,
            function.name,
            customer_id=customer_id,
            exclude_function_id=function.id,
        )
        function.status = ContactStatus.ACTIVE
        function.deleted_at = None
        function.deleted_by = None
        function.updated_by = context.user_id
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as error:
            raise _name_exists_error() from error
        await session.refresh(function)
        await session.commit()
    return function


async def list_customer_recycle_functions(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
) -> list[ContactFunction]:
    await _require_visible_active_customer(session, customer_id, context)
    statement = (
        select(ContactFunction)
        .where(
            ContactFunction.customer_id == customer_id,
            ContactFunction.status == ContactStatus.DELETED,
        )
        .order_by(ContactFunction.name, ContactFunction.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_contacts(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[tuple[CustomerContact, ContactFunction]], int]:
    await _require_visible_active_customer(session, customer_id, context)
    return await contact_repository.list_contacts(
        session,
        customer_id,
        status=ContactStatus.ACTIVE,
        page=page,
        page_size=page_size,
    )


async def list_deleted_contacts(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[tuple[CustomerContact, ContactFunction]], int]:
    await _require_visible_active_customer(session, customer_id, context)
    return await contact_repository.list_contacts(
        session,
        customer_id,
        status=ContactStatus.DELETED,
        page=page,
        page_size=page_size,
    )


@audited(
    action="contact:create",
    resource_type="customer_contact",
    resource_id_attr="id",
)
async def create_contact(
    session: AsyncSession,
    customer_id: int,
    payload: ContactCreate,
    *,
    context: AuthContext,
) -> CustomerContact:
    await _require_visible_active_customer(session, customer_id, context)

    changes: dict[str, object] = payload.model_dump()
    function_id = changes.pop("function_id")
    changes = _validate_contact_fields(changes)
    if not isinstance(function_id, int):
        raise _invalid_contact_field_error("Contact function is required")
    await _assert_contact_function_usable(session, customer_id, function_id)

    contact = CustomerContact(
        customer_id=customer_id,
        function_id=function_id,
        status=ContactStatus.ACTIVE,
        **changes,
    )
    session.add(contact)
    await session.flush()
    await session.refresh(contact)
    await session.commit()
    return contact


@audited(
    action="contact:update",
    resource_type="customer_contact",
    resource_id_param="contact_id",
)
async def update_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    payload: ContactUpdate,
    *,
    context: AuthContext,
) -> CustomerContact:
    await _require_visible_active_customer(session, customer_id, context)
    contact = await _require_active_contact(session, customer_id, contact_id)
    changes: dict[str, object] = payload.model_dump(exclude_unset=True)
    changes = _validate_contact_fields(changes)

    if "function_id" in changes:
        function_id = changes["function_id"]
        if not isinstance(function_id, int):
            raise _invalid_contact_field_error("Contact function is required")
        await _assert_contact_function_usable(session, customer_id, function_id)

    for field_name, value in changes.items():
        setattr(contact, field_name, value)

    await session.flush()
    await session.refresh(contact)
    await session.commit()
    return contact


@audited(
    action="contact:delete",
    resource_type="customer_contact",
    resource_id_param="contact_id",
)
async def delete_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    *,
    context: AuthContext,
) -> None:
    await _require_visible_active_customer(session, customer_id, context)
    contact = await _require_active_contact(session, customer_id, contact_id)

    contact.status = ContactStatus.DELETED
    contact.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    contact.deleted_by = context.user_id
    await session.flush()
    await session.commit()


@audited(
    action="contact:restore",
    resource_type="customer_contact",
    resource_id_param="contact_id",
)
async def restore_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    *,
    context: AuthContext,
) -> CustomerContact:
    await _require_visible_active_customer(session, customer_id, context)
    contact = await contact_repository.get_contact(session, customer_id, contact_id)
    if contact is None:
        raise _contact_not_found_error()
    if contact.status != ContactStatus.DELETED:
        raise _contact_not_deleted_error()

    contact.status = ContactStatus.ACTIVE
    contact.deleted_at = None
    contact.deleted_by = None
    await session.flush()
    await session.refresh(contact)
    await session.commit()
    return contact


__all__: tuple[str, ...] = (
    "create_contact",
    "create_custom_function",
    "create_shared_function",
    "delete_contact",
    "delete_custom_function",
    "delete_shared_function",
    "list_contacts",
    "list_customer_functions",
    "list_customer_recycle_functions",
    "list_deleted_contacts",
    "list_shared_functions",
    "restore_contact",
    "restore_custom_function",
    "restore_shared_function",
    "update_contact",
    "update_custom_function",
    "update_shared_function",
)
