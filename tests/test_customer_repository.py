"""客户仓储层测试。"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.customer import Customer, CustomerSource
from app.models.department import Department, DepartmentStatus
from app.models.user import User, UserStatus
from app.repositories.customer import (
    add,
    credit_codes_existing,
    delete,
    get_by_credit_code,
    get_by_id,
    list_customers,
)

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


async def make_department(
    session: AsyncSession,
    *,
    name: str | None = None,
    is_system: bool = False,
) -> Department:
    department = Department(
        parent_id=None,
        name=name or f"部门-{uuid4().hex[:8]}",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=is_system,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.flush()
    return department


async def make_user(
    session: AsyncSession,
    *,
    department: Department | None = None,
) -> User:
    user = User(
        username=f"user-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
        department_id=department.id if department is not None else None,
    )
    session.add(user)
    await session.flush()
    return user


def make_customer(
    owner: User,
    *,
    name: str | None = None,
    short_name: str | None = None,
    credit_code: str | None = None,
    source: CustomerSource = CustomerSource.MANUAL,
) -> Customer:
    return Customer(
        name=name or f"客户-{uuid4().hex[:8]}",
        short_name=short_name,
        credit_code=credit_code,
        source=source,
        owner_user_id=owner.id,
        owner_department_id=owner.department_id,
    )


async def test_credit_code_is_unique(session: AsyncSession) -> None:
    owner = await make_user(session)
    await add(session, make_customer(owner, credit_code="911100007693890511"))
    with pytest.raises(IntegrityError):
        await add(session, make_customer(owner, credit_code="911100007693890511"))


async def test_null_credit_codes_do_not_conflict(session: AsyncSession) -> None:
    owner = await make_user(session)
    await add(session, make_customer(owner, credit_code=None))
    await add(session, make_customer(owner, credit_code=None))


async def test_get_by_credit_code_returns_none_for_missing_code(
    session: AsyncSession,
) -> None:
    assert await get_by_credit_code(session, "91110000MA00000099") is None


async def test_get_by_id_and_delete_roundtrip(session: AsyncSession) -> None:
    owner = await make_user(session)
    customer = await add(
        session,
        make_customer(owner, credit_code="91110000MA00000001"),
    )

    loaded = await get_by_id(session, customer.id)
    assert loaded is not None
    assert loaded.id == customer.id

    await delete(session, loaded)
    await session.flush()

    remaining = await session.scalar(
        select(Customer).where(Customer.id == customer.id)
    )
    assert remaining is None


async def test_business_scope_truncated_defaults_to_false(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    customer = await add(session, make_customer(owner))
    assert customer.business_scope_truncated is False


async def test_owner_department_id_may_be_null(session: AsyncSession) -> None:
    owner = await make_user(session)
    customer = await add(session, make_customer(owner))
    assert customer.owner_department_id is None


async def test_list_customers_filters_keyword_source_and_scope(
    session: AsyncSession,
) -> None:
    department = await make_department(session)
    owner = await make_user(session, department=department)
    other_owner = await make_user(session)

    matched = await add(
        session,
        make_customer(
            owner,
            name="北京天眼查科技有限公司",
            short_name="天眼查",
            credit_code="91110000MA00000010",
            source=CustomerSource.TIANYANCHA,
        ),
    )
    await add(session, make_customer(owner, name="手工录入客户", short_name="手工"))
    await add(session, make_customer(other_owner, name="他人客户", short_name="他人"))

    keyword_rows, keyword_total = await list_customers(
        session,
        page=1,
        page_size=10,
        scope_filter=true(),
        keyword="天眼",
        source=None,
    )
    assert keyword_total == 1
    assert [row.id for row in keyword_rows] == [matched.id]

    source_rows, source_total = await list_customers(
        session,
        page=1,
        page_size=10,
        scope_filter=true(),
        keyword=None,
        source=CustomerSource.TIANYANCHA,
    )
    assert source_total == 1
    assert [row.id for row in source_rows] == [matched.id]

    scoped_rows, scoped_total = await list_customers(
        session,
        page=1,
        page_size=10,
        scope_filter=Customer.owner_user_id == owner.id,
        keyword=None,
        source=None,
    )
    assert scoped_total == 2
    assert matched.id in [row.id for row in scoped_rows]
    assert all(row.owner_user_id == owner.id for row in scoped_rows)


async def test_credit_codes_existing_handles_empty_and_multiple_codes(
    session: AsyncSession,
) -> None:
    assert await credit_codes_existing(session, []) == {}

    owner = await make_user(session)
    first = await add(
        session,
        make_customer(owner, credit_code="91110000MA00000021"),
    )
    second = await add(
        session,
        make_customer(owner, credit_code="91110000MA00000022"),
    )
    await add(session, make_customer(owner, credit_code=None))

    found = await credit_codes_existing(
        session,
        [
            "91110000MA00000021",
            "91110000MA00000022",
            "91110000MA00000023",
        ],
    )

    assert set(found) == {"91110000MA00000021", "91110000MA00000022"}
    assert found["91110000MA00000021"].id == first.id
    assert found["91110000MA00000022"].id == second.id
