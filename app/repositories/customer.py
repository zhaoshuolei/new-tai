"""客户数据访问。"""

from datetime import UTC, datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.contact import ContactStatus
from app.models.customer import Customer, CustomerSource


async def get_by_id(session: AsyncSession, customer_id: int) -> Customer | None:
    return await session.get(Customer, customer_id)


async def get_by_credit_code(
    session: AsyncSession,
    credit_code: str,
    *,
    for_update: bool = False,
) -> Customer | None:
    """按信用代码查未删除客户。

    ``for_update=True`` 用于唯一键冲突后的复核：MySQL REPEATABLE READ 下普通
    SELECT 会固定事务快照，并发事务刚提交的冲突行不可见；当前读才能看到它。
    """
    statement = select(Customer).where(
        Customer.credit_code == credit_code,
        Customer.status == ContactStatus.ACTIVE,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_by_credit_code_any_status(
    session: AsyncSession,
    credit_code: str,
) -> Customer | None:
    statement = (
        select(Customer)
        .where(Customer.credit_code == credit_code)
        .order_by(
            case((Customer.status == ContactStatus.ACTIVE, 0), else_=1),
            Customer.id,
        )
        .limit(1)
    )
    return await session.scalar(statement)


async def add(session: AsyncSession, customer: Customer) -> Customer:
    session.add(customer)
    await session.flush()
    # 刷新以加载服务端生成的 created_at/updated_at，避免序列化时异步懒加载
    # 触发 MissingGreenlet（与 integration_config.upsert 一致）。
    await session.refresh(customer)
    return customer


async def soft_delete(
    session: AsyncSession,
    customer: Customer,
    *,
    deleted_by: int,
) -> None:
    customer.status = ContactStatus.DELETED
    customer.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    customer.deleted_by = deleted_by


async def restore(session: AsyncSession, customer: Customer) -> None:
    customer.status = ContactStatus.ACTIVE
    customer.deleted_at = None
    customer.deleted_by = None


async def has_customers(session: AsyncSession, user_id: int) -> bool:
    statement = select(Customer.id).where(Customer.owner_user_id == user_id).limit(1)
    return await session.scalar(statement) is not None


async def has_customers_for_update(session: AsyncSession, user_id: int) -> bool:
    """以加锁读（当前读）判断用户是否仍拥有客户。

    MySQL 默认 REPEATABLE READ：普通 SELECT 会固定事务快照，并发提交的客户行
    可能不可见；`FOR UPDATE` 是当前读，会读取最新已提交的数据。删除用户后发生
    外键冲突时用它复核归属，避免漏判导致裸 IntegrityError 冒泡成 500。
    """
    statement = (
        select(Customer.id).where(Customer.owner_user_id == user_id).with_for_update()
    )
    return (await session.scalars(statement)).first() is not None


async def list_customers(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    scope_filter: ColumnElement[bool],
    keyword: str | None,
    source: CustomerSource | None,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> tuple[list[Customer], int]:
    conditions: list[ColumnElement[bool]] = [
        scope_filter,
        Customer.status == status,
    ]
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
    statement = select(Customer).where(
        Customer.credit_code.in_(codes),
        Customer.status == ContactStatus.ACTIVE,
    )
    result = await session.scalars(statement)
    return {
        customer.credit_code: customer
        for customer in result.all()
        if customer.credit_code is not None
    }


__all__: tuple[str, ...] = (
    "add",
    "credit_codes_existing",
    "get_by_credit_code",
    "get_by_credit_code_any_status",
    "get_by_id",
    "has_customers",
    "has_customers_for_update",
    "list_customers",
    "restore",
    "soft_delete",
)
