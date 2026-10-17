"""Customer soft-delete, restore, credit-code, transfer, and recycle-bin behavior."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.role import DataScope
from app.models.user import User
from app.repositories import customer as customer_repository
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.services import customer as customer_service
from app.services.customer import (
    create_customer,
    delete_customer,
    get_customer,
    import_from_tianyancha,
    list_deleted_customers,
    restore_customer,
    transfer_owner,
    update_customer,
)
from app.services.user import delete_user
from tests.conftest import FakeRedis
from tests.customer_helpers import (  # noqa: F401
    FakeTianyanchaClient,
    api_client,
    make_context,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)
CODE = "91110000MA00000001"


@pytest.fixture
def fake_client() -> FakeTianyanchaClient:
    return FakeTianyanchaClient()


@pytest.fixture(autouse=True)
def _inject_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: FakeTianyanchaClient,
) -> None:
    async def _get_client(_session: AsyncSession) -> FakeTianyanchaClient:
        return fake_client

    monkeypatch.setattr(customer_service, "get_tianyancha_client", _get_client)


async def test_delete_is_soft_and_hidden_from_get(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)

    await delete_customer(session, customer.id, context=ctx)

    row = await session.scalar(select(Customer).where(Customer.id == customer.id))
    assert row is not None
    assert row.status == ContactStatus.DELETED
    assert row.deleted_by == owner.id
    assert row.deleted_at is not None
    with pytest.raises(BusinessError) as exc:
        await get_customer(session, customer.id, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_restore_reactivates_and_clears_deletion_fields(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(session, customer.id, context=ctx)

    restored = await restore_customer(session, customer.id, context=ctx)

    assert restored.status == ContactStatus.ACTIVE
    assert restored.deleted_at is None
    assert restored.deleted_by is None


async def test_restore_active_customer_returns_7012(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, customer.id, context=ctx)
    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_restore_conflicts_when_active_customer_reuses_code(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)
    active = await make_customer_row(session, owner=owner, credit_code=CODE)

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, deleted.id, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": active.id}


async def test_restore_race_translates_integrity_error_to_7002(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)
    active = await make_customer_row(session, owner=owner, credit_code=CODE)

    original_lookup = customer_repository.get_by_credit_code
    lookup_count = 0

    async def stale_then_current(
        current_session: AsyncSession,
        credit_code: str,
    ) -> Customer | None:
        nonlocal lookup_count
        if credit_code == CODE and lookup_count == 0:
            lookup_count += 1
            return None
        return await original_lookup(current_session, credit_code)

    monkeypatch.setattr(
        customer_repository,
        "get_by_credit_code",
        stale_then_current,
    )

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, deleted.id, context=ctx)

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": active.id}


async def test_restore_invisible_or_missing_customer_returns_7001(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    deleted = await make_customer_row(session, owner=owner)
    await delete_customer(session, deleted.id, context=make_context(owner))
    viewer = await make_user(session)
    visible_all = make_context(viewer, is_super_admin=True)
    invisible = make_context(viewer, data_scope=DataScope.SELF)

    for customer_id, ctx in ((deleted.id, invisible), (999_999_999, visible_all)):
        with pytest.raises(BusinessError) as exc:
            await restore_customer(session, customer_id, context=ctx)
        assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_create_with_deleted_credit_code_returns_7011_without_id(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="New customer", credit_code=CODE),
            context=ctx,
        )
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS
    assert exc.value.extra is None


async def test_import_with_deleted_credit_code_does_not_call_tianyancha(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, CODE, None, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS
    assert fake_client.calls == []


async def test_update_to_deleted_credit_code_returns_7011(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)
    target = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await update_customer(
            session,
            target.id,
            CustomerUpdate(credit_code=CODE),
            context=ctx,
        )
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS
    assert exc.value.extra is None


async def _make_deleted_then_active_with_same_code(
    session: AsyncSession,
    owner: User,
) -> Customer:
    context = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=context)
    return await make_customer_row(session, owner=owner, credit_code=CODE)


async def test_create_prefers_active_when_deleted_has_same_credit_code(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    active = await _make_deleted_then_active_with_same_code(session, owner)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="Conflicting customer", credit_code=CODE),
            context=ctx,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": active.id}


async def test_import_prefers_active_when_deleted_has_same_credit_code(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    active = await _make_deleted_then_active_with_same_code(session, owner)

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, CODE, None, context=ctx)

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": active.id}
    assert fake_client.calls == []


async def test_update_prefers_active_when_deleted_has_same_credit_code(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    active = await _make_deleted_then_active_with_same_code(session, owner)
    target = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await update_customer(
            session,
            target.id,
            CustomerUpdate(credit_code=CODE),
            context=ctx,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": active.id}


async def test_update_to_own_credit_code_succeeds(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner, credit_code=CODE)

    updated = await update_customer(
        session,
        customer.id,
        CustomerUpdate(credit_code=CODE),
        context=ctx,
    )

    assert updated.id == customer.id
    assert updated.credit_code == CODE


async def test_transfer_active_customer_needs_update_permission(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await transfer_owner(
            session,
            customer.id,
            target.id,
            context=make_context(owner, permission_codes={"customer:delete"}),
        )
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert exc.value.http_status == 403


async def test_transfer_deleted_customer_needs_delete_permission(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(
        session,
        customer.id,
        context=make_context(owner, permission_codes={"customer:delete"}),
    )

    update_only = make_context(owner, permission_codes={"customer:update"})
    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, target.id, context=update_only)
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert exc.value.http_status == 403


async def test_transfer_deleted_customer_keeps_deleted_status(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    await delete_customer(session, customer.id, context=ctx)

    moved = await transfer_owner(session, customer.id, target.id, context=ctx)
    assert moved.owner_user_id == target.id
    assert moved.status == ContactStatus.DELETED


async def test_super_admin_can_transfer_deleted_customer(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(session, customer.id, context=make_context(owner))

    moved = await transfer_owner(
        session,
        customer.id,
        target.id,
        context=make_context(owner, is_super_admin=True),
    )
    assert moved.owner_user_id == target.id
    assert moved.status == ContactStatus.DELETED


async def test_user_with_deleted_customer_blocked_until_transferred(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    owner = await make_user(session)
    heir = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    await delete_customer(session, customer.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await delete_user(
            session,
            owner.id,
            context=make_context(heir, is_super_admin=True),
            redis=fake_redis,
        )
    assert exc.value.code == ErrorCode.USER_HAS_CUSTOMERS

    await transfer_owner(session, customer.id, heir.id, context=ctx)
    await delete_user(
        session,
        owner.id,
        context=make_context(heir, is_super_admin=True),
        redis=fake_redis,
    )
    remaining = await session.scalar(select(User).where(User.id == owner.id))
    assert remaining is None


async def test_recycle_bin_lists_only_deleted_within_scope(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    hidden_owner = await make_user(session)
    ctx = make_context(
        owner,
        permission_codes={"customer:delete"},
        data_scope=DataScope.SELF,
    )
    active = await make_customer_row(session, owner=owner)
    deleted = await make_customer_row(session, owner=owner)
    await delete_customer(session, deleted.id, context=ctx)
    hidden = await make_customer_row(session, owner=hidden_owner)
    await delete_customer(session, hidden.id, context=make_context(hidden_owner))

    items, total = await list_deleted_customers(
        session,
        context=ctx,
        page=1,
        page_size=20,
    )
    assert total == 1
    assert [c.id for c in items] == [deleted.id]
    assert active.id not in [c.id for c in items]
    assert hidden.id not in [c.id for c in items]


async def test_recycle_bin_route_precedes_customer_id_and_restore_route(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(session, customer.id, context=ctx)

    async with api_client(session, ctx) as client:
        recycle_bin = await client.get("/api/v1/customers/recycle-bin")
        restored = await client.post(f"/api/v1/customers/{customer.id}/restore")

    assert recycle_bin.status_code == 200, recycle_bin.text
    assert [item["id"] for item in recycle_bin.json()["data"]["items"]] == [customer.id]
    assert restored.status_code == 200, restored.text
    row = await session.get(Customer, customer.id)
    assert row is not None
    assert row.status == ContactStatus.ACTIVE


async def test_deleted_owner_transfer_route_uses_service_status_permission(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    await delete_customer(session, customer.id, context=ctx)

    async with api_client(session, ctx) as client:
        response = await client.patch(
            f"/api/v1/customers/{customer.id}/owner",
            json={"owner_user_id": target.id},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["owner_user_id"] == target.id
    row = await session.get(Customer, customer.id)
    assert row is not None
    assert row.status == ContactStatus.DELETED
