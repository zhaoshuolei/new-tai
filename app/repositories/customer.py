"""客户数据访问。"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.customer import Customer, CustomerSource


async def get_by_id(session: AsyncSession, customer_id: int) -> Customer | None:
    return await session.get(Customer, customer_id)


async def get_by_credit_code(
    session: AsyncSession,
    credit_code: str,
) -> Customer | None:
    statement = select(Customer).where(Customer.credit_code == credit_code)
    return await session.scalar(statement)


async def add(session: AsyncSession, customer: Customer) -> Customer:
    session.add(customer)
    await session.flush()
    # 刷新以加载服务端生成的 created_at/updated_at，避免序列化时异步懒加载
    # 触发 MissingGreenlet（与 integration_config.upsert 一致）。
    await session.refresh(customer)
    return customer


async def delete(session: AsyncSession, customer: Customer) -> None:
    await session.delete(customer)


async def list_customers(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    scope_filter: ColumnElement[bool],
    keyword: str | None,
    source: CustomerSource | None,
) -> tuple[list[Customer], int]:
    conditions: list[ColumnElement[bool]] = [scope_filter]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                Customer.name.like(pattern),
                Customer.short_name.like(pattern),
                Customer.credit_code.like(pattern),
            )
        )
    if source is not None:
        conditions.append(Customer.source == source)

    total_statement = select(func.count()).select_from(Customer).where(*conditions)
    total = await session.scalar(total_statement)

    statement = (
        select(Customer)
        .where(*conditions)
        .order_by(Customer.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.scalars(statement)
    return list(result.all()), int(total or 0)


async def credit_codes_existing(
    session: AsyncSession,
    codes: list[str],
) -> dict[str, Customer]:
    if not codes:
        return {}
    statement = select(Customer).where(Customer.credit_code.in_(codes))
    result = await session.scalars(statement)
    return {
        customer.credit_code: customer
        for customer in result.all()
        if customer.credit_code is not None
    }


__all__: tuple[str, ...] = (
    "add",
    "credit_codes_existing",
    "delete",
    "get_by_credit_code",
    "get_by_id",
    "list_customers",
)
