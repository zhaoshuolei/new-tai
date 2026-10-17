import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog
from app.models.contact import ContactFunction, ContactStatus, CustomerContact
from app.models.customer import Customer
from app.models.department import Department  # noqa: F401
from app.models.role import DataScope
from app.models.user import User
from app.schemas.contact import ContactCreate
from app.services.contact import create_contact, delete_shared_function
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_customer_row,
    make_user,
)


async def test_reciprocal_function_deletes_use_ordered_locks() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    customer_id: int | None = None
    function_ids: list[int] = []

    try:
        async with factory() as setup:
            user = await make_user(setup)
            customer = await make_customer_row(setup, owner=user)
            x = await add_function(
                setup,
                name=f"reciprocal-x-{user.id}",
                customer_id=None,
            )
            y = await add_function(
                setup,
                name=f"reciprocal-y-{user.id}",
                customer_id=None,
            )
            await add_contact(setup, customer, x, name="x-contact")
            await add_contact(setup, customer, y, name="y-contact")
            await setup.commit()
            user_id = user.id
            customer_id = customer.id
            x_id = x.id
            y_id = y.id
            function_ids = [x_id, y_id]

        context = AuthContext(
            user_id=user_id,
            role_id=1,
            department_id=None,
            data_scope=DataScope.ALL,
            custom_department_ids=frozenset(),
            include_children=False,
            permission_codes=frozenset({"customer:delete"}),
            is_super_admin=True,
        )
        ready = 0
        both_ready = asyncio.Event()
        ready_lock = asyncio.Lock()

        async def delete_pair(target_id: int, replacement_id: int) -> str:
            nonlocal ready
            async with factory() as session:
                async with ready_lock:
                    ready += 1
                    if ready == 2:
                        both_ready.set()
                await both_ready.wait()
                try:
                    await delete_shared_function(
                        session,
                        target_id,
                        replacement_function_id=replacement_id,
                        context=context,
                    )
                except BusinessError as error:
                    return f"err:{error.code}"
                return "ok"

        results = await asyncio.wait_for(
            asyncio.gather(
                delete_pair(x_id, y_id),
                delete_pair(y_id, x_id),
                return_exceptions=True,
            ),
            timeout=10,
        )

        assert all(isinstance(result, str) for result in results), results
        assert results.count("ok") == 1
        errors = [result for result in results if result != "ok"]
        assert errors in (
            [f"err:{ErrorCode.CONTACT_FUNCTION_NOT_FOUND.value}"],
            [f"err:{ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED.value}"],
            [f"err:{ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID.value}"],
        )

        async with factory() as check:
            dangling_ids = (
                await check.scalars(
                    select(CustomerContact.id)
                    .join(
                        ContactFunction,
                        CustomerContact.function_id == ContactFunction.id,
                    )
                    .where(
                        ContactFunction.id.in_(function_ids),
                        ContactFunction.status == ContactStatus.DELETED,
                    )
                )
            ).all()
            assert dangling_ids == []
            functions = (
                await check.scalars(
                    select(ContactFunction)
                    .where(ContactFunction.id.in_(function_ids))
                    .order_by(ContactFunction.id)
                )
            ).all()
            assert [function.status for function in functions] == [
                ContactStatus.DELETED,
                ContactStatus.ACTIVE,
            ] or [function.status for function in functions] == [
                ContactStatus.ACTIVE,
                ContactStatus.DELETED,
            ]
    finally:
        try:
            async with factory() as cleanup:
                if user_id is not None:
                    await cleanup.execute(
                        delete(AuditLog).where(AuditLog.user_id == user_id)
                    )
                if customer_id is not None:
                    await cleanup.execute(
                        delete(CustomerContact).where(
                            CustomerContact.customer_id == customer_id
                        )
                    )
                if function_ids:
                    await cleanup.execute(
                        delete(ContactFunction).where(
                            ContactFunction.id.in_(function_ids)
                        )
                    )
                if customer_id is not None:
                    await cleanup.execute(
                        delete(Customer).where(Customer.id == customer_id)
                    )
                if user_id is not None:
                    await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
        finally:
            await engine.dispose()


async def test_concurrent_create_and_function_delete_leave_no_dangling_reference() -> (
    None
):
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    customer_id: int | None = None
    f_id: int | None = None
    g_id: int | None = None
    created_contact_id: int | None = None

    try:
        async with factory() as setup:
            user = await make_user(setup)
            customer = await make_customer_row(setup, owner=user)
            f = await add_function(
                setup,
                name=f"concurrency-target-{user.id}",
                customer_id=None,
            )
            g = await add_function(
                setup,
                name=f"concurrency-replacement-{user.id}",
                customer_id=None,
            )
            await add_contact(setup, customer, f, name="anchor-contact")
            fixture_user_id = user.id
            fixture_customer_id = customer.id
            fixture_f_id = f.id
            fixture_g_id = g.id
            user_id = fixture_user_id
            customer_id = fixture_customer_id
            f_id = fixture_f_id
            g_id = fixture_g_id
            await setup.commit()

        context = AuthContext(
            user_id=fixture_user_id,
            role_id=1,
            department_id=None,
            data_scope=DataScope.ALL,
            custom_department_ids=frozenset(),
            include_children=False,
            permission_codes=frozenset({"customer:update", "customer:delete"}),
            is_super_admin=True,
        )

        async def create() -> str:
            nonlocal created_contact_id
            async with factory() as session:
                try:
                    contact = await create_contact(
                        session,
                        fixture_customer_id,
                        ContactCreate(
                            name="concurrent-contact",
                            function_id=fixture_f_id,
                        ),
                        context=context,
                    )
                except BusinessError as error:
                    return f"err:{error.code}"
                created_contact_id = contact.id
                return "ok"

        async def delete_function() -> str:
            async with factory() as session:
                try:
                    await delete_shared_function(
                        session,
                        fixture_f_id,
                        replacement_function_id=fixture_g_id,
                        context=context,
                    )
                except BusinessError as error:
                    return f"err:{error.code}"
                return "ok"

        create_task = asyncio.create_task(create())
        delete_task = asyncio.create_task(delete_function())
        create_result, delete_result = await asyncio.gather(
            create_task,
            delete_task,
            return_exceptions=True,
        )
        for result in (create_result, delete_result):
            if isinstance(result, BaseException):
                raise result

        async with factory() as check:
            dangling_ids = (
                await check.scalars(
                    select(CustomerContact.id)
                    .join(
                        ContactFunction,
                        CustomerContact.function_id == ContactFunction.id,
                    )
                    .where(
                        ContactFunction.id == fixture_f_id,
                        ContactFunction.status == ContactStatus.DELETED,
                    )
                )
            ).all()

            assert dangling_ids == []
            if create_result == "ok":
                assert created_contact_id is not None
                created_contact = await check.scalar(
                    select(CustomerContact).where(
                        CustomerContact.id == created_contact_id
                    )
                )
                assert created_contact is not None
                assert created_contact.function_id == fixture_g_id

        assert delete_result == "ok"
        assert create_result in (
            "ok",
            f"err:{ErrorCode.CONTACT_FUNCTION_UNAVAILABLE.value}",
        )
    finally:
        try:
            async with factory() as cleanup:
                if user_id is not None:
                    await cleanup.execute(
                        delete(AuditLog).where(AuditLog.user_id == user_id)
                    )
                if customer_id is not None:
                    await cleanup.execute(
                        delete(CustomerContact).where(
                            CustomerContact.customer_id == customer_id
                        )
                    )
                function_ids = [item for item in (f_id, g_id) if item is not None]
                if function_ids:
                    await cleanup.execute(
                        delete(ContactFunction).where(
                            ContactFunction.id.in_(function_ids)
                        )
                    )
                if customer_id is not None:
                    await cleanup.execute(
                        delete(Customer).where(Customer.id == customer_id)
                    )
                if user_id is not None:
                    await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
        finally:
            await engine.dispose()
