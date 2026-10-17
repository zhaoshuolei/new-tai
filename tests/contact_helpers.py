"""对接人与职能测试的数据构造函数。签名在此固定，测试只能从本模块导入。"""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import ContactFunction, ContactStatus, CustomerContact
from app.models.customer import Customer


async def add_function(
    session: AsyncSession,
    *,
    name: str,
    customer_id: int | None,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> ContactFunction:
    """写入职能。

    customer_id 为 None 即共用职能（scope_key=0）；否则为自定义职能
    （scope_key=customer_id）。
    """
    function = ContactFunction(
        name=name,
        customer_id=customer_id,
        scope_key=0 if customer_id is None else customer_id,
        status=status,
    )
    session.add(function)
    await session.flush()
    return function


async def add_contact(
    session: AsyncSession,
    customer: Customer,
    function: ContactFunction,
    *,
    name: str | None = None,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> CustomerContact:
    contact = CustomerContact(
        customer_id=customer.id,
        function_id=function.id,
        name=name or f"联系人-{uuid4().hex[:8]}",
        status=status,
    )
    session.add(contact)
    await session.flush()
    return contact


__all__: tuple[str, ...] = ("add_contact", "add_function")
