from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.contact import ContactFunction, ContactStatus
from app.repositories import contact as contact_repository
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)


class _RecordingResult:
    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    statement: Any = None

    async def scalar(self, statement: Any) -> None:
        self.statement = statement
        return None

    async def scalars(self, statement: Any) -> _RecordingResult:
        self.statement = statement
        return _RecordingResult()


async def test_list_functions_for_customer_includes_shared_and_own_only(
    session,
) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    customer_b = await make_customer_row(session, owner=owner)
    await add_function(session, name="商务", customer_id=None)
    await add_function(session, name="渠道对接", customer_id=customer_a.id)
    await add_function(session, name="他客户的", customer_id=customer_b.id)
    await add_function(
        session, name="已删旧", customer_id=None, status=ContactStatus.DELETED
    )

    names = [
        f.name
        for f in await contact_repository.list_functions_for_customer(
            session, customer_a.id
        )
    ]
    assert names == ["商务", "渠道对接"]


async def test_count_contacts_includes_deleted_contacts(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, customer_a, fn, name="张三", status=ContactStatus.ACTIVE)
    await add_contact(
        session, customer_a, fn, name="李四", status=ContactStatus.DELETED
    )

    assert await contact_repository.count_contacts_referencing(session, fn.id) == 2


async def test_reassign_moves_all_contacts_including_deleted(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    src = await add_function(session, name="研发", customer_id=None)
    dst = await add_function(session, name="产品", customer_id=None)
    await add_contact(
        session, customer_a, src, name="张三", status=ContactStatus.ACTIVE
    )
    await add_contact(
        session, customer_a, src, name="李四", status=ContactStatus.DELETED
    )

    moved = await contact_repository.reassign_contacts(session, src.id, dst.id)
    assert moved == 2
    assert await contact_repository.count_contacts_referencing(session, src.id) == 0
    assert await contact_repository.count_contacts_referencing(session, dst.id) == 2


async def test_list_contacts_paginates_and_orders(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn_b = await add_function(session, name="运营", customer_id=None)
    fn_a = await add_function(session, name="商务", customer_id=None)
    for name in ["王五", "张三", "赵六"]:
        await add_contact(session, customer_a, fn_a, name=name)
    await add_contact(session, customer_a, fn_b, name="孙七")
    await add_contact(session, customer_a, fn_b, name="钱八")

    items, total = await contact_repository.list_contacts(
        session,
        customer_a.id,
        status=ContactStatus.ACTIVE,
        page=1,
        page_size=3,
    )
    assert total == 5
    assert [c.name for c, _ in items] == ["张三", "王五", "赵六"]


async def test_list_contacts_excludes_deleted(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn = await add_function(session, name="商务", customer_id=None)
    await add_contact(session, customer_a, fn, name="在岗", status=ContactStatus.ACTIVE)
    await add_contact(
        session, customer_a, fn, name="离岗", status=ContactStatus.DELETED
    )

    items, total = await contact_repository.list_contacts(
        session,
        customer_a.id,
        status=ContactStatus.ACTIVE,
        page=1,
        page_size=20,
    )
    assert total == 1
    assert items[0][0].name == "在岗"


@pytest.mark.parametrize(
    "lock_function",
    [
        contact_repository.lock_function_for_update,
        contact_repository.lock_function_shared,
    ],
)
async def test_lock_functions_refresh_stale_identity_map(lock_function) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    function_id: int | None = None
    try:
        async with factory() as setup:
            function = ContactFunction(
                name=f"锁测试-{uuid4().hex}",
                customer_id=None,
                scope_key=0,
                status=ContactStatus.ACTIVE,
            )
            setup.add(function)
            await setup.commit()
            created_id = function.id
            function_id = created_id

        async with factory() as reader:
            stale = await contact_repository.get_function(reader, created_id)
            assert stale is not None
            assert stale.status == ContactStatus.ACTIVE

            async with factory() as writer:
                current = await writer.get(ContactFunction, created_id)
                assert current is not None
                current.status = ContactStatus.DELETED
                await writer.commit()

            locked = await lock_function(reader, created_id)
            assert locked is not None
            assert locked.status == ContactStatus.DELETED
    finally:
        if function_id is not None:
            async with factory() as cleanup:
                function = await cleanup.get(ContactFunction, function_id)
                if function is not None:
                    await cleanup.delete(function)
                    await cleanup.commit()
        await engine.dispose()


async def test_lock_function_shared_uses_shared_lock() -> None:
    recording = _RecordingSession()

    await contact_repository.lock_function_shared(
        cast(AsyncSession, recording),
        10,
    )

    compiled = str(
        recording.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LOCK IN SHARE MODE" in compiled


@pytest.mark.parametrize(
    "lock_function",
    [
        contact_repository.lock_function_for_update,
        contact_repository.lock_function_shared,
    ],
)
async def test_lock_functions_return_none_for_missing_id(
    session, lock_function
) -> None:
    assert await lock_function(session, -1) is None
