"""联系人职能与客户联系人数据访问。"""

from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import ContactFunction, ContactStatus, CustomerContact


async def get_function(
    session: AsyncSession, function_id: int
) -> ContactFunction | None:
    return await session.get(ContactFunction, function_id)


async def list_functions_for_customer(
    session: AsyncSession,
    customer_id: int,
) -> list[ContactFunction]:
    statement = (
        select(ContactFunction)
        .where(
            or_(
                ContactFunction.customer_id.is_(None),
                ContactFunction.customer_id == customer_id,
            ),
            ContactFunction.status == ContactStatus.ACTIVE,
        )
        .order_by(ContactFunction.name, ContactFunction.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def list_shared_functions(
    session: AsyncSession,
    *,
    status: ContactStatus,
) -> list[ContactFunction]:
    statement = (
        select(ContactFunction)
        .where(
            ContactFunction.customer_id.is_(None),
            ContactFunction.status == status,
        )
        .order_by(ContactFunction.name, ContactFunction.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def lock_function_for_update(
    session: AsyncSession,
    function_id: int,
) -> ContactFunction | None:
    statement = (
        select(ContactFunction)
        .where(ContactFunction.id == function_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def lock_function_shared(
    session: AsyncSession,
    function_id: int,
) -> ContactFunction | None:
    statement = (
        select(ContactFunction)
        .where(ContactFunction.id == function_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def lock_functions_for_update(
    session: AsyncSession,
    function_ids: set[int],
) -> list[ContactFunction]:
    if not function_ids:
        return []
    statement = (
        select(ContactFunction)
        .where(ContactFunction.id.in_(sorted(function_ids)))
        .order_by(ContactFunction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def count_contacts_referencing(
    session: AsyncSession,
    function_id: int,
) -> int:
    statement = (
        select(func.count())
        .select_from(CustomerContact)
        .where(CustomerContact.function_id == function_id)
    )
    total = await session.scalar(statement)
    return int(total or 0)


async def lock_contact_ids_referencing(
    session: AsyncSession,
    function_id: int,
) -> list[int]:
    """以当前读取出引用该职能的对接人 ID，并锁住这些行。

    删除职能在 REPEATABLE READ 下不能使用普通 COUNT：事务快照可能早于并发
    创建的对接人提交，计数会漏掉它。``FOR UPDATE`` 是当前读，且与对接人写入
    （先共享锁职能行、再插行）共用"先职能后对接人"的加锁顺序。
    """
    statement = (
        select(CustomerContact.id)
        .where(CustomerContact.function_id == function_id)
        .order_by(CustomerContact.id)
        .with_for_update()
    )
    result = await session.scalars(statement)
    return list(result.all())


async def reassign_contacts(
    session: AsyncSession,
    from_function_id: int,
    to_function_id: int,
) -> int:
    statement = (
        update(CustomerContact)
        .where(CustomerContact.function_id == from_function_id)
        .values(function_id=to_function_id)
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    return int(result.rowcount or 0)


async def list_contacts(
    session: AsyncSession,
    customer_id: int,
    *,
    status: ContactStatus,
    page: int,
    page_size: int,
) -> tuple[list[tuple[CustomerContact, ContactFunction]], int]:
    conditions = (
        CustomerContact.customer_id == customer_id,
        CustomerContact.status == status,
    )
    total_statement = (
        select(func.count())
        .select_from(CustomerContact)
        .join(ContactFunction, CustomerContact.function_id == ContactFunction.id)
        .where(*conditions)
    )
    total = await session.scalar(total_statement)

    statement = (
        select(CustomerContact, ContactFunction)
        .join(ContactFunction, CustomerContact.function_id == ContactFunction.id)
        .where(*conditions)
        .order_by(
            ContactFunction.name,
            CustomerContact.name,
            CustomerContact.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(statement)
    items = [(contact, function) for contact, function in result.all()]
    return items, int(total or 0)


async def get_contact(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
) -> CustomerContact | None:
    statement = select(CustomerContact).where(
        CustomerContact.customer_id == customer_id,
        CustomerContact.id == contact_id,
    )
    return await session.scalar(statement)


__all__: tuple[str, ...] = (
    "count_contacts_referencing",
    "get_contact",
    "get_function",
    "list_contacts",
    "list_functions_for_customer",
    "list_shared_functions",
    "lock_contact_ids_referencing",
    "lock_function_for_update",
    "lock_function_shared",
    "lock_functions_for_update",
    "reassign_contacts",
)
