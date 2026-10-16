"""客户业务逻辑：检索、天眼查回填、CRUD 与负责人转移。

服务层不 commit，事务边界由调用方控制；只 flush。
"""

import re
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tianyancha import parse_change_scope_records
from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.customer import Customer, CustomerSource
from app.models.user import User, UserStatus
from app.repositories import customer as customer_repository
from app.repositories.customer import get_by_credit_code
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.services.audit import audited
from app.services.customer_scope_text import resolve_full_business_scope
from app.services.data_scope import (
    apply_customer_scope,
    apply_user_scope,
    build_customer_scope_filter,
)
from app.services.integration import get_tianyancha_client

_CREDIT_CODE_PATTERN = re.compile(r"^[0-9A-Z]{18}$")

_STRING_UPDATE_FIELDS = (
    "short_name",
    "legal_person",
    "registered_capital",
    "reg_status",
    "business_scope",
    "remark",
)


def _invalid_field_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_INVALID_FIELD,
        message,
        http_status=422,
    )


def _not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_NOT_FOUND,
        "Customer not found",
        http_status=404,
    )


def _owner_unavailable_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CUSTOMER_OWNER_UNAVAILABLE,
        "Owner is unavailable",
        http_status=422,
    )


def _normalize_credit_code(value: str) -> str:
    code = value.strip().upper()
    if _CREDIT_CODE_PATTERN.fullmatch(code) is None:
        raise _invalid_field_error("Invalid unified social credit code")
    return code


def _normalize_optional_credit_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not code:
        return None
    if _CREDIT_CODE_PATTERN.fullmatch(code) is None:
        raise _invalid_field_error("Invalid unified social credit code")
    return code


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_established_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


async def _visible_customer_ids(
    session: AsyncSession,
    customer_ids: list[int],
    context: AuthContext,
) -> set[int]:
    if not customer_ids:
        return set()
    statement = apply_customer_scope(
        select(Customer.id).where(Customer.id.in_(customer_ids)),
        context,
    )
    result = await session.scalars(statement)
    return {customer_id for customer_id in result.all()}


async def _duplicate_error(
    session: AsyncSession,
    existing: Customer,
    context: AuthContext,
) -> BusinessError:
    visible_ids = await _visible_customer_ids(session, [existing.id], context)
    extra: dict[str, object] | None = None
    if existing.id in visible_ids:
        extra = {"existing_customer_id": existing.id}
    return BusinessError(
        ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS,
        "Unified social credit code already exists",
        http_status=409,
        extra=extra,
    )


async def _get_scoped_customer(
    session: AsyncSession,
    customer_id: int,
    context: AuthContext,
) -> Customer | None:
    statement = apply_customer_scope(
        select(Customer).where(Customer.id == customer_id),
        context,
    )
    result = await session.scalar(statement)
    return result if isinstance(result, Customer) else None


async def _require_scoped_customer(
    session: AsyncSession,
    customer_id: int,
    context: AuthContext,
) -> Customer:
    customer = await _get_scoped_customer(session, customer_id, context)
    if customer is None:
        raise _not_found_error()
    return customer


async def _insert_customer(
    session: AsyncSession,
    customer: Customer,
    *,
    credit_code: str | None,
    context: AuthContext,
) -> None:
    try:
        async with session.begin_nested():
            await customer_repository.add(session, customer)
    except IntegrityError as error:
        if credit_code is not None:
            existing = await customer_repository.get_by_credit_code(
                session, credit_code
            )
            if existing is not None:
                duplicate = await _duplicate_error(session, existing, context)
                raise duplicate from error
        raise


async def search_candidates(
    session: AsyncSession,
    keyword: str,
    *,
    page: int,
    page_size: int,
    context: AuthContext,
) -> dict[str, object]:
    text = keyword.strip()
    if not text:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Keyword must not be empty",
            http_status=422,
        )

    client = await get_tianyancha_client(session)
    candidates, total = await client.search_candidates(
        text,
        page=page,
        page_size=page_size,
    )

    codes = [
        candidate.credit_code
        for candidate in candidates
        if candidate.credit_code is not None
    ]
    existing = await customer_repository.credit_codes_existing(session, codes)
    visible_ids = await _visible_customer_ids(
        session,
        [row.id for row in existing.values()],
        context,
    )

    items: list[dict[str, object]] = []
    for candidate in candidates:
        item: dict[str, object] = {
            "company_id": candidate.company_id,
            "name": candidate.name,
            "credit_code": candidate.credit_code,
            "legal_person": candidate.legal_person,
            "reg_status": candidate.reg_status,
        }
        if candidate.credit_code is not None:
            row = existing.get(candidate.credit_code)
            if row is None:
                item["existing_customer_id"] = None
            elif row.id in visible_ids:
                item["existing_customer_id"] = row.id
            else:
                item["existed_hidden"] = True
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@audited(
    action="customer:create",
    resource_type="customer",
    resource_id_attr="id",
)
async def import_from_tianyancha(
    session: AsyncSession,
    credit_code: str,
    short_name: str | None,
    *,
    context: AuthContext,
) -> Customer:
    code = _normalize_credit_code(credit_code)

    existing = await customer_repository.get_by_credit_code(session, code)
    if existing is not None:
        duplicate = await _duplicate_error(session, existing, context)
        raise duplicate

    client = await get_tianyancha_client(session)
    registration = await client.fetch_registration(code)
    if registration.credit_code.strip().upper() != code:
        raise BusinessError(
            ErrorCode.TIANYANCHA_MALFORMED_RESPONSE,
            "Tianyancha returned a mismatched credit code",
            http_status=502,
        )

    historical_text = await client.fetch_historical_registration_text(code)
    records = parse_change_scope_records(historical_text)
    business_scope, truncated = resolve_full_business_scope(
        registration.business_scope,
        records,
    )

    customer = Customer(
        name=registration.name,
        short_name=_clean_optional(short_name),
        credit_code=code,
        legal_person=registration.legal_person,
        registered_capital=registration.registered_capital,
        established_date=_parse_established_date(registration.established_date),
        business_scope=business_scope,
        business_scope_truncated=truncated,
        reg_status=registration.reg_status,
        source=CustomerSource.TIANYANCHA,
        tyc_imported_at=datetime.now(UTC).replace(tzinfo=None),
        remark=None,
        owner_user_id=context.user_id,
        owner_department_id=context.department_id,
    )
    await _insert_customer(
        session,
        customer,
        credit_code=code,
        context=context,
    )
    return customer


@audited(
    action="customer:create",
    resource_type="customer",
    resource_id_attr="id",
)
async def create_customer(
    session: AsyncSession,
    payload: CustomerCreate,
    *,
    context: AuthContext,
) -> Customer:
    name = payload.name.strip()
    if not name:
        raise _invalid_field_error("Customer name is required")

    code = _normalize_optional_credit_code(payload.credit_code)
    if code is not None:
        existing = await customer_repository.get_by_credit_code(session, code)
        if existing is not None:
            duplicate = await _duplicate_error(session, existing, context)
            raise duplicate

    customer = Customer(
        name=name,
        short_name=_clean_optional(payload.short_name),
        credit_code=code,
        legal_person=_clean_optional(payload.legal_person),
        registered_capital=_clean_optional(payload.registered_capital),
        established_date=payload.established_date,
        business_scope=_clean_optional(payload.business_scope),
        business_scope_truncated=False,
        reg_status=_clean_optional(payload.reg_status),
        source=CustomerSource.MANUAL,
        tyc_imported_at=None,
        remark=_clean_optional(payload.remark),
        owner_user_id=context.user_id,
        owner_department_id=context.department_id,
    )
    await _insert_customer(
        session,
        customer,
        credit_code=code,
        context=context,
    )
    return customer


async def get_customer(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
) -> Customer:
    return await _require_scoped_customer(session, customer_id, context)


@audited(
    action="customer:update",
    resource_type="customer",
    resource_id_param="customer_id",
)
async def update_customer(
    session: AsyncSession,
    customer_id: int,
    payload: CustomerUpdate,
    *,
    context: AuthContext,
) -> Customer:
    customer = await _require_scoped_customer(session, customer_id, context)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise _invalid_field_error("Customer name is required")
        changes["name"] = name

    updated_credit_code: str | None = None
    if "credit_code" in changes:
        code = _normalize_optional_credit_code(changes["credit_code"])
        changes["credit_code"] = code
        if code is not None and code != customer.credit_code:
            existing = await get_by_credit_code(session, code)
            if existing is not None and existing.id != customer.id:
                duplicate = await _duplicate_error(session, existing, context)
                raise duplicate
        updated_credit_code = code

    for field_name in _STRING_UPDATE_FIELDS:
        if field_name in changes:
            changes[field_name] = _clean_optional(changes[field_name])

    if updated_credit_code is not None:
        # 主动查重与写入之间可能有并发提交；用 SAVEPOINT 把唯一约束冲突
        # 转为 7002，避免裸 IntegrityError 冒泡为 500（与 _insert_customer 对齐）。
        # 赋值与 flush 同在 SAVEPOINT 内，回滚后对象恢复旧值，不会在随后的
        # 冲突查重中再次 autoflush 触发同一异常。
        try:
            async with session.begin_nested():
                for field_name, value in changes.items():
                    setattr(customer, field_name, value)
                await session.flush()
        except IntegrityError as error:
            conflicting = await customer_repository.get_by_credit_code(
                session, updated_credit_code
            )
            if conflicting is not None:
                duplicate = await _duplicate_error(session, conflicting, context)
                raise duplicate from error
            raise
    else:
        for field_name, value in changes.items():
            setattr(customer, field_name, value)
        await session.flush()
    return customer


@audited(
    action="customer:delete",
    resource_type="customer",
    resource_id_param="customer_id",
)
async def delete_customer(
    session: AsyncSession,
    customer_id: int,
    *,
    context: AuthContext,
) -> None:
    customer = await _require_scoped_customer(session, customer_id, context)
    await customer_repository.delete(session, customer)
    await session.flush()


@audited(
    action="customer:transfer",
    resource_type="customer",
    resource_id_param="customer_id",
)
async def transfer_owner(
    session: AsyncSession,
    customer_id: int,
    owner_user_id: int,
    *,
    context: AuthContext,
) -> Customer:
    customer = await _require_scoped_customer(session, customer_id, context)

    target = await session.get(User, owner_user_id)
    if target is None or target.status != UserStatus.ACTIVE:
        raise _owner_unavailable_error()

    visible = await session.scalar(
        apply_user_scope(select(User.id).where(User.id == owner_user_id), context)
    )
    if visible is None:
        raise _owner_unavailable_error()

    customer.owner_user_id = target.id
    customer.owner_department_id = target.department_id
    await session.flush()
    return customer


async def list_customers(
    session: AsyncSession,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
    keyword: str | None = None,
    source: CustomerSource | None = None,
) -> tuple[list[Customer], int]:
    return await customer_repository.list_customers(
        session,
        page=page,
        page_size=page_size,
        scope_filter=build_customer_scope_filter(context),
        keyword=keyword,
        source=source,
    )


__all__: tuple[str, ...] = (
    "create_customer",
    "delete_customer",
    "get_customer",
    "import_from_tianyancha",
    "list_customers",
    "search_candidates",
    "transfer_owner",
    "update_customer",
)
