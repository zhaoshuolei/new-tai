"""联系人职能业务逻辑：共用/自定义维护、删除迁移与恢复。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactFunction, ContactStatus
from app.models.customer import Customer
from app.repositories import contact as contact_repository
from app.services.audit import audited
from app.services.data_scope import apply_customer_scope

FunctionScope = Literal["SHARED", "CUSTOM"]
_NAME_LOCK_NAME = "contact_function_name"
_NAME_LOCK_TIMEOUT_SECONDS = 5.0


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


def _normalize_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise _invalid_name_error("Contact function name is required")
    if len(value) > 64:
        raise _invalid_name_error("Contact function name must not exceed 64 characters")
    return value


def _name_change_concurrent_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONTACT_FUNCTION_NAME_EXISTS,
        "Contact function name is being changed concurrently",
        http_status=409,
    )


def _require_super_admin(context: AuthContext) -> None:
    if not context.is_super_admin:
        raise _forbidden_error()


def _async_engine_for(session: AsyncSession) -> AsyncEngine:
    # AsyncSession.get_bind() exposes SQLAlchemy's synchronous engine/connection
    # mirror, so consult the async bind to open a truly independent connection.
    bind = session.get_bind()
    if isinstance(bind, AsyncConnection):
        return bind.engine
    if isinstance(bind, AsyncEngine):
        return bind

    session_bind = session.bind
    if isinstance(session_bind, AsyncConnection):
        return session_bind.engine
    if isinstance(session_bind, AsyncEngine):
        return session_bind
    raise TypeError("AsyncSession must be bound to an async engine or connection")


@asynccontextmanager
async def _serialize_name_change(session: AsyncSession) -> AsyncIterator[None]:
    engine = _async_engine_for(session)
    async with engine.connect() as lock_connection:
        try:
            acquired = await lock_connection.scalar(
                text("SELECT GET_LOCK(:lock_name, :timeout)"),
                {
                    "lock_name": _NAME_LOCK_NAME,
                    "timeout": _NAME_LOCK_TIMEOUT_SECONDS,
                },
            )
            if acquired != 1:
                raise _name_change_concurrent_error()
            yield
        except BaseException:
            await session.rollback()
            raise
        finally:
            await lock_connection.scalar(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": _NAME_LOCK_NAME},
            )


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


__all__: tuple[str, ...] = (
    "create_custom_function",
    "create_shared_function",
    "delete_custom_function",
    "delete_shared_function",
    "list_customer_functions",
    "list_customer_recycle_functions",
    "list_shared_functions",
    "restore_custom_function",
    "restore_shared_function",
    "update_custom_function",
    "update_shared_function",
)
