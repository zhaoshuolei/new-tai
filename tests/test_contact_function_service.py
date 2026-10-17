import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog, AuditResult
from app.models.contact import (
    ContactFunction,
    ContactFunctionNameLock,
    ContactStatus,
)
from app.models.customer import Customer
from app.models.user import User
from app.services import audit as audit_service
from app.services.contact import (
    create_custom_function,
    create_shared_function,
    delete_custom_function,
    delete_shared_function,
    list_customer_functions,
    list_customer_recycle_functions,
    list_shared_functions,
    restore_custom_function,
    restore_shared_function,
    update_custom_function,
    update_shared_function,
)
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_context,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)


async def _assert_name_lock_row_available(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as probe:
        lock_id = await probe.scalar(
            select(ContactFunctionNameLock.id)
            .where(ContactFunctionNameLock.id == 1)
            .with_for_update(nowait=True)
        )
        assert lock_id == 1
        await probe.rollback()


@pytest_asyncio.fixture
async def isolated_audit_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest.mark.parametrize("operation", ["update", "delete", "restore"])
async def test_non_super_admin_cannot_write_shared_function(
    session,
    operation: str,
) -> None:
    user = await make_user(session)
    context = make_context(user, permission_codes={"customer:update"})
    function = await add_function(session, name="商务", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        if operation == "update":
            await update_shared_function(
                session,
                function.id,
                "运营",
                context=context,
            )
        elif operation == "delete":
            await delete_shared_function(
                session,
                function.id,
                replacement_function_id=None,
                context=context,
            )
        else:
            await restore_shared_function(
                session,
                function.id,
                context=context,
            )

    assert exc.value.http_status == 403


async def test_non_super_admin_cannot_list_shared_recycle_bin(session) -> None:
    user = await make_user(session)
    context = make_context(user, permission_codes={"customer:delete"})

    with pytest.raises(BusinessError) as exc:
        await list_shared_functions(
            session,
            context=context,
            include_deleted=True,
        )

    assert exc.value.http_status == 403


async def test_shared_list_filters_deleted_unless_recycle_bin(session) -> None:
    user = await make_user(session)
    context = make_context(user, is_super_admin=True)
    active = await add_function(session, name="商务", customer_id=None)
    deleted = await add_function(
        session,
        name="旧职能",
        customer_id=None,
        status=ContactStatus.DELETED,
    )

    active_rows = await list_shared_functions(session, context=context)
    all_rows = await list_shared_functions(
        session,
        context=context,
        include_deleted=True,
    )

    assert [row.id for row in active_rows] == [active.id]
    assert {row.id for row in all_rows} == {active.id, deleted.id}


async def test_shared_name_clashing_with_active_custom_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="渠道对接", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await create_shared_function(
            session,
            "渠道对接",
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_custom_name_clashing_with_shared_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="商务", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await create_custom_function(
            session,
            owner.id,
            "商务",
            context=make_context(user, permission_codes={"customer:update"}),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_shared_create_rejects_custom_name_from_another_customer(
    session,
) -> None:
    user = await make_user(session)
    other_owner = await make_customer_row(session, owner=user)
    await add_function(session, name="渠道", customer_id=other_owner.id)

    with pytest.raises(BusinessError) as exc:
        await create_shared_function(
            session,
            "渠道",
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_custom_create_rejects_existing_shared_name_for_any_customer(
    session,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="商务", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await create_custom_function(
            session,
            owner.id,
            "商务",
            context=make_context(user, permission_codes={"customer:update"}),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_engine_bound_create_commits_and_releases_name_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    user_id: int | None = None
    customer_id: int | None = None
    function_id: int | None = None
    name = f"生产职能-{uuid4().hex}"

    try:
        async with factory() as setup:
            user = await make_user(setup)
            customer = await make_customer_row(setup, owner=user)
            await setup.commit()
            user_id = user.id
            customer_id = customer.id

        async with factory() as app_session:
            created = await create_shared_function(
                app_session,
                name,
                context=make_context(user, is_super_admin=True),
            )
            function_id = created.id

        async with factory() as verify:
            row = await verify.scalar(
                select(ContactFunction).where(ContactFunction.id == function_id)
            )
        assert row is not None
        assert row.name == name
        await _assert_name_lock_row_available(factory)
    finally:
        async with factory() as cleanup:
            if function_id is not None:
                await cleanup.execute(
                    delete(ContactFunction).where(ContactFunction.id == function_id)
                )
            await cleanup.execute(
                delete(ContactFunction).where(ContactFunction.name == name)
            )
            if user_id is not None:
                await cleanup.execute(
                    delete(AuditLog).where(AuditLog.user_id == user_id)
                )
            if customer_id is not None:
                await cleanup.execute(
                    delete(Customer).where(Customer.id == customer_id)
                )
            if user_id is not None:
                await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
        await engine.dispose()


async def test_engine_bound_conflict_does_not_leak_name_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    user_id: int | None = None
    customer_id: int | None = None
    function_id: int | None = None
    name = f"生产冲突-{uuid4().hex}"

    try:
        async with factory() as setup:
            user = await make_user(setup)
            customer = await make_customer_row(setup, owner=user)
            existing = ContactFunction(
                name=name,
                customer_id=None,
                scope_key=0,
                status=ContactStatus.ACTIVE,
            )
            setup.add(existing)
            await setup.commit()
            user_id = user.id
            customer_id = customer.id
            function_id = existing.id

        async with factory() as app_session:
            with pytest.raises(BusinessError) as exc:
                await create_shared_function(
                    app_session,
                    name,
                    context=make_context(user, is_super_admin=True),
                )
        assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS
        await _assert_name_lock_row_available(factory)
    finally:
        async with factory() as cleanup:
            if function_id is not None:
                await cleanup.execute(
                    delete(ContactFunction).where(ContactFunction.id == function_id)
                )
            if user_id is not None:
                await cleanup.execute(
                    delete(AuditLog).where(AuditLog.user_id == user_id)
                )
            if customer_id is not None:
                await cleanup.execute(
                    delete(Customer).where(Customer.id == customer_id)
                )
            if user_id is not None:
                await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
        await engine.dispose()


async def test_cross_scope_name_writes_are_serialized() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    customer_id: int | None = None
    function_id: int | None = None
    sessions: list[AsyncSession] = []
    attempt: asyncio.Future[ContactFunction] | None = None
    lock_attempted = asyncio.Event()
    loop = asyncio.get_running_loop()
    name = f"并发职能-{uuid4().hex}"

    def on_before_cursor_execute(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _execution_context: object,
        _executemany: bool,
    ) -> None:
        if (
            "contact_function_name_lock" in statement
            and "FOR UPDATE" in statement.upper()
        ):
            loop.call_soon_threadsafe(lock_attempted.set)

    try:
        async with factory() as setup:
            user = await make_user(setup)
            customer = await make_customer_row(setup, owner=user)
            await setup.commit()
            user_id = user.id
            customer_id = customer.id

        context = make_context(
            user,
            permission_codes={"customer:update"},
        )
        session_a = factory()
        session_b = factory()
        sessions.extend((session_a, session_b))
        assert (
            await session_b.scalar(
                select(Customer.id).where(Customer.id == customer.id)
            )
            == customer.id
        )
        assert (
            await session_a.scalar(
                select(ContactFunctionNameLock.id)
                .where(ContactFunctionNameLock.id == 1)
                .with_for_update()
            )
            == 1
        )

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            on_before_cursor_execute,
        )
        try:
            attempt = asyncio.ensure_future(
                create_custom_function(
                    session_b,
                    customer.id,
                    name,
                    context=context,
                )
            )
            await asyncio.wait_for(lock_attempted.wait(), timeout=3.0)
            assert not attempt.done()

            shared = ContactFunction(
                name=name,
                customer_id=None,
                scope_key=0,
                status=ContactStatus.ACTIVE,
                created_by=user.id,
                updated_by=user.id,
            )
            session_a.add(shared)
            await session_a.commit()
            function_id = shared.id

            with pytest.raises(BusinessError) as exc:
                await asyncio.wait_for(attempt, timeout=3.0)
            assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                on_before_cursor_execute,
            )

        async with factory() as verify:
            assert (
                await verify.scalar(
                    select(ContactFunction).where(ContactFunction.id == function_id)
                )
            ) is not None
        await _assert_name_lock_row_available(factory)
    finally:
        if attempt is not None and not attempt.done():
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        for open_session in sessions:
            await open_session.close()
        async with factory() as cleanup:
            if function_id is not None:
                await cleanup.execute(
                    delete(ContactFunction).where(ContactFunction.id == function_id)
                )
            await cleanup.execute(
                delete(ContactFunction).where(ContactFunction.name == name)
            )
            if customer_id is not None:
                await cleanup.execute(
                    delete(ContactFunction).where(
                        ContactFunction.customer_id == customer_id
                    )
                )
            if user_id is not None:
                await cleanup.execute(
                    delete(AuditLog).where(AuditLog.user_id == user_id)
                )
            if customer_id is not None:
                await cleanup.execute(
                    delete(Customer).where(Customer.id == customer_id)
                )
            if user_id is not None:
                await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.parametrize("creator", ["shared", "custom"])
async def test_rename_to_own_current_name_succeeds(
    session,
    creator: str,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    if creator == "shared":
        function = await add_function(session, name="商务", customer_id=None)
    else:
        function = await add_function(
            session,
            name="渠道",
            customer_id=owner.id,
        )
    original_name = function.name

    if creator == "shared":
        renamed = await update_shared_function(
            session,
            function.id,
            original_name,
            context=make_context(user, is_super_admin=True),
        )
    else:
        renamed = await update_custom_function(
            session,
            owner.id,
            function.id,
            original_name,
            context=make_context(
                user,
                permission_codes={"customer:update"},
            ),
        )

    assert renamed.id == function.id
    assert renamed.name == original_name


async def test_missing_name_lock_row_fails_loudly(session) -> None:
    user = await make_user(session)
    await session.execute(
        delete(ContactFunctionNameLock).where(ContactFunctionNameLock.id == 1)
    )

    with pytest.raises(RuntimeError, match="contact_function_name_lock"):
        await create_shared_function(
            session,
            "缺少锁行",
            context=make_context(user, is_super_admin=True),
        )


async def test_shared_rename_clashing_with_custom_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    custom = await add_function(session, name="渠道对接", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await update_shared_function(
            session,
            shared.id,
            custom.name,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_custom_rename_clashing_with_shared_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    custom = await add_function(session, name="渠道对接", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await update_custom_function(
            session,
            owner.id,
            custom.id,
            shared.name,
            context=make_context(user, permission_codes={"customer:update"}),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_deleted_function_does_not_reserve_name(session) -> None:
    user = await make_user(session)
    await add_function(
        session,
        name="商务",
        customer_id=None,
        status=ContactStatus.DELETED,
    )

    created = await create_shared_function(
        session,
        "商务",
        context=make_context(user, is_super_admin=True),
    )

    assert created.status == ContactStatus.ACTIVE


@pytest.mark.parametrize("creator", ["shared", "custom"])
@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
async def test_function_name_validation_returns_7003(
    session,
    creator: str,
    name: str,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)

    with pytest.raises(BusinessError) as exc:
        if creator == "shared":
            await create_shared_function(
                session,
                name,
                context=make_context(user, is_super_admin=True),
            )
        else:
            await create_custom_function(
                session,
                owner.id,
                name,
                context=make_context(
                    user,
                    permission_codes={"customer:update"},
                ),
            )

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_function_name_is_trimmed_on_create_and_rename(session) -> None:
    user = await make_user(session)
    context = make_context(user, is_super_admin=True)

    created = await create_shared_function(
        session,
        "  商务  ",
        context=context,
    )
    created_name = created.name
    renamed = await update_shared_function(
        session,
        created.id,
        "  运营  ",
        context=context,
    )

    assert created_name == "商务"
    assert renamed.name == "运营"


async def test_list_customer_functions_labels_shared_and_custom(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    other_owner = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    own = await add_function(session, name="渠道", customer_id=owner.id)
    await add_function(session, name="外部", customer_id=other_owner.id)
    await add_function(
        session,
        name="删除项",
        customer_id=owner.id,
        status=ContactStatus.DELETED,
    )

    rows = await list_customer_functions(
        session,
        owner.id,
        context=make_context(user, permission_codes={"customer:read"}),
    )

    assert {(row.id, scope) for row, scope in rows} == {
        (shared.id, "SHARED"),
        (own.id, "CUSTOM"),
    }


async def test_list_customer_recycle_bin_returns_only_own_deleted(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    other_owner = await make_customer_row(session, owner=user)
    own_deleted = await add_function(
        session,
        name="自建旧项",
        customer_id=owner.id,
        status=ContactStatus.DELETED,
    )
    await add_function(
        session,
        name="他人旧项",
        customer_id=other_owner.id,
        status=ContactStatus.DELETED,
    )
    await add_function(
        session,
        name="共用旧项",
        customer_id=None,
        status=ContactStatus.DELETED,
    )

    rows = await list_customer_recycle_functions(
        session,
        owner.id,
        context=make_context(user, permission_codes={"customer:delete"}),
    )

    assert [row.id for row in rows] == [own_deleted.id]


async def test_delete_without_contacts_needs_no_replacement(session) -> None:
    user = await make_user(session)
    function = await add_function(session, name="临时", customer_id=None)

    moved = await delete_shared_function(
        session,
        function.id,
        replacement_function_id=None,
        context=make_context(user, is_super_admin=True),
    )

    row = await session.scalar(
        select(ContactFunction).where(ContactFunction.id == function.id)
    )
    assert moved == 0
    assert row is not None
    assert row.status == ContactStatus.DELETED


async def test_delete_with_contacts_requires_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    function = await add_function(session, name="研发", customer_id=None)
    contact = await add_contact(session, owner, function, name="张三")

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=None,
            context=make_context(user, is_super_admin=True),
        )

    row = await session.scalar(
        select(ContactFunction).where(ContactFunction.id == function.id)
    )
    await session.refresh(contact)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED
    assert row is not None
    assert row.status == ContactStatus.ACTIVE
    assert contact.function_id == function.id


async def test_delete_without_contacts_rejects_replacement(session) -> None:
    user = await make_user(session)
    function = await add_function(session, name="研发", customer_id=None)
    other = await add_function(session, name="产品", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=other.id,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_missing_replacement_without_contacts_returns_7009(
    session,
) -> None:
    user = await make_user(session)
    function = await add_function(session, name="研发", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=-1,
            context=make_context(user, is_super_admin=True),
        )

    await session.refresh(function)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID
    assert function.status == ContactStatus.ACTIVE


async def test_missing_replacement_with_contacts_returns_7009_and_keeps_contact(
    session,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    function = await add_function(session, name="研发", customer_id=None)
    contact = await add_contact(session, owner, function, name="张三")

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=-1,
            context=make_context(user, is_super_admin=True),
        )

    await session.refresh(function)
    await session.refresh(contact)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID
    assert function.status == ContactStatus.ACTIVE
    assert contact.function_id == function.id


async def test_shared_delete_rejects_custom_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    function = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, owner, function, name="张三")
    custom = await add_function(session, name="渠道", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=custom.id,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_shared_delete_rejects_deleted_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    function = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, owner, function, name="张三")
    gone = await add_function(
        session,
        name="旧",
        customer_id=None,
        status=ContactStatus.DELETED,
    )

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(
            session,
            function.id,
            replacement_function_id=gone.id,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


@pytest.mark.parametrize("replacement_kind", ["shared", "own"])
async def test_custom_delete_allows_shared_or_own_replacement(
    session,
    replacement_kind: str,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    context = make_context(
        user,
        permission_codes={"customer:update", "customer:delete"},
    )
    target = await add_function(session, name="渠道", customer_id=owner.id)
    own_replacement = await add_function(
        session,
        name="渠道二",
        customer_id=owner.id,
    )
    shared_replacement = await add_function(
        session,
        name="商务",
        customer_id=None,
    )
    contact = await add_contact(session, owner, target, name="A")
    replacement = (
        shared_replacement if replacement_kind == "shared" else own_replacement
    )

    moved = await delete_custom_function(
        session,
        owner.id,
        target.id,
        replacement_function_id=replacement.id,
        context=context,
    )

    await session.refresh(contact)
    await session.refresh(target)
    assert moved == 1
    assert contact.function_id == replacement.id
    assert target.status == ContactStatus.DELETED


async def test_custom_delete_rejects_other_customer_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    other_owner = await make_customer_row(session, owner=user)
    context = make_context(
        user,
        permission_codes={"customer:update", "customer:delete"},
    )
    own = await add_function(session, name="渠道", customer_id=owner.id)
    foreign = await add_function(
        session,
        name="外部",
        customer_id=other_owner.id,
    )
    await add_contact(session, owner, own, name="A")

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(
            session,
            owner.id,
            own.id,
            replacement_function_id=foreign.id,
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_delete_rejects_self_as_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    context = make_context(
        user,
        permission_codes={"customer:update", "customer:delete"},
    )
    own = await add_function(session, name="渠道", customer_id=owner.id)
    await add_contact(session, owner, own, name="A")

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(
            session,
            owner.id,
            own.id,
            replacement_function_id=own.id,
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_delete_migrates_soft_deleted_contacts_too(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    source = await add_function(session, name="研发", customer_id=None)
    destination = await add_function(session, name="产品", customer_id=None)
    gone = await add_contact(
        session,
        owner,
        source,
        name="离岗",
        status=ContactStatus.DELETED,
    )

    moved = await delete_shared_function(
        session,
        source.id,
        replacement_function_id=destination.id,
        context=make_context(user, is_super_admin=True),
    )

    await session.refresh(gone)
    assert moved == 1
    assert gone.function_id == destination.id


async def test_restore_does_not_move_contacts_back(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    source = await add_function(session, name="研发", customer_id=None)
    destination = await add_function(session, name="产品", customer_id=None)
    contact = await add_contact(session, owner, source, name="张三")
    context = make_context(user, is_super_admin=True)
    await delete_shared_function(
        session,
        source.id,
        replacement_function_id=destination.id,
        context=context,
    )

    restored = await restore_shared_function(
        session,
        source.id,
        context=context,
    )

    await session.refresh(contact)
    assert restored.status == ContactStatus.ACTIVE
    assert contact.function_id == destination.id


async def test_restore_shared_name_conflict_returns_7007(session) -> None:
    user = await make_user(session)
    gone = await add_function(
        session,
        name="研发",
        customer_id=None,
        status=ContactStatus.DELETED,
    )
    await add_function(session, name="研发", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await restore_shared_function(
            session,
            gone.id,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_restore_active_function_returns_7012(session) -> None:
    user = await make_user(session)
    function = await add_function(session, name="研发", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await restore_shared_function(
            session,
            function.id,
            context=make_context(user, is_super_admin=True),
        )

    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_functions_of_deleted_customer_are_not_visible(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="渠道", customer_id=owner.id)
    owner.status = ContactStatus.DELETED
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await list_customer_functions(
            session,
            owner.id,
            context=make_context(user, permission_codes={"customer:read"}),
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_restore_custom_function_of_deleted_customer_returns_7001(
    session,
) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    function = await add_function(
        session,
        name="渠道",
        customer_id=owner.id,
        status=ContactStatus.DELETED,
    )
    owner.status = ContactStatus.DELETED
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await restore_custom_function(
            session,
            owner.id,
            function.id,
            context=make_context(user, permission_codes={"customer:delete"}),
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_custom_function_belonging_to_other_customer_returns_7006(
    session,
) -> None:
    user = await make_user(session)
    customer_a = await make_customer_row(session, owner=user)
    customer_b = await make_customer_row(session, owner=user)
    foreign = await add_function(
        session,
        name="外部",
        customer_id=customer_b.id,
    )
    context = make_context(
        user,
        permission_codes={"customer:delete", "customer:update"},
    )

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(
            session,
            customer_a.id,
            foreign.id,
            replacement_function_id=None,
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NOT_FOUND


async def test_delete_shared_function_writes_audit_row(
    session,
    isolated_audit_writes: async_sessionmaker[AsyncSession],
) -> None:
    user = await make_user(session)
    function = await add_function(session, name="临时", customer_id=None)

    await delete_shared_function(
        session,
        function.id,
        replacement_function_id=None,
        context=make_context(user, is_super_admin=True),
    )

    async with isolated_audit_writes() as audit_session:
        row = await audit_session.scalar(
            select(AuditLog).where(
                AuditLog.action == "contact_function:delete",
                AuditLog.resource_id == function.id,
            )
        )

    assert row is not None
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "contact_function"
    assert row.user_id == user.id


async def test_create_update_restore_write_audit_metadata(
    session,
    isolated_audit_writes: async_sessionmaker[AsyncSession],
) -> None:
    user = await make_user(session)
    context = make_context(user, is_super_admin=True)
    function = await create_shared_function(session, "审计创建", context=context)
    await update_shared_function(
        session,
        function.id,
        "审计更新",
        context=context,
    )
    await delete_shared_function(
        session,
        function.id,
        replacement_function_id=None,
        context=context,
    )
    await restore_shared_function(session, function.id, context=context)

    async with isolated_audit_writes() as audit_session:
        rows = (
            await audit_session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.resource_type == "contact_function",
                    AuditLog.resource_id == function.id,
                )
                .order_by(AuditLog.id)
            )
        ).all()

    actions = [row.action for row in rows]
    assert actions == [
        "contact_function:create",
        "contact_function:update",
        "contact_function:delete",
        "contact_function:restore",
    ]
    assert all(row.resource_type == "contact_function" for row in rows)
    assert all(row.resource_id == function.id for row in rows)
    assert all(row.result is AuditResult.SUCCESS for row in rows)
