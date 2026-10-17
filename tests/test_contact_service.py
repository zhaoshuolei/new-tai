import pytest
from pydantic import ValidationError

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.role import DataScope
from app.schemas.contact import ContactCreate, ContactUpdate
from app.services.contact import (
    create_contact,
    delete_contact,
    list_contacts,
    restore_contact,
    update_contact,
)
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    make_context,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)

PERMS = {"customer:read", "customer:update", "customer:delete"}


async def _setup(session):
    user = await make_user(session)
    context = make_context(user, permission_codes=PERMS)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    return user, context, customer, shared


async def test_create_with_shared_function_succeeds(session) -> None:
    _, context, customer, shared = await _setup(session)

    created = await create_contact(
        session,
        customer.id,
        ContactCreate(name="张三", function_id=shared.id),
        context=context,
    )

    assert created.id is not None
    assert created.function_id == shared.id
    assert created.status == ContactStatus.ACTIVE


async def test_create_with_other_customer_function_returns_7010(session) -> None:
    user, context, customer, _ = await _setup(session)
    other = await make_customer_row(session, owner=user)
    foreign = await add_function(session, name="外部", customer_id=other.id)

    with pytest.raises(BusinessError) as exc:
        await create_contact(
            session,
            customer.id,
            ContactCreate(name="张三", function_id=foreign.id),
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_create_with_deleted_function_returns_7010(session) -> None:
    _, context, customer, _ = await _setup(session)
    gone = await add_function(
        session,
        name="旧",
        customer_id=None,
        status=ContactStatus.DELETED,
    )

    with pytest.raises(BusinessError) as exc:
        await create_contact(
            session,
            customer.id,
            ContactCreate(name="张三", function_id=gone.id),
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_invalid_phone_returns_7003(session) -> None:
    _, context, customer, shared = await _setup(session)

    with pytest.raises(BusinessError) as exc:
        await create_contact(
            session,
            customer.id,
            ContactCreate(name="张三", function_id=shared.id, phone="12345"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_invalid_email_returns_7003(session) -> None:
    _, context, customer, shared = await _setup(session)

    with pytest.raises(BusinessError) as exc:
        await create_contact(
            session,
            customer.id,
            ContactCreate(name="张三", function_id=shared.id, email="no-at"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_blank_name_returns_7003(session) -> None:
    _, context, customer, shared = await _setup(session)

    with pytest.raises(BusinessError) as exc:
        await create_contact(
            session,
            customer.id,
            ContactCreate(name="   ", function_id=shared.id),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_blank_optional_strings_are_stored_as_none(session) -> None:
    _, context, customer, shared = await _setup(session)

    created = await create_contact(
        session,
        customer.id,
        ContactCreate(
            name="张三",
            function_id=shared.id,
            position="  ",
            phone="  ",
            email="",
            home_address=" ",
            remark="",
        ),
        context=context,
    )

    assert created.position is None
    assert created.phone is None
    assert created.email is None
    assert created.home_address is None
    assert created.remark is None


async def test_update_changes_function_to_shared(session) -> None:
    _, context, customer, shared = await _setup(session)
    other_shared = await add_function(session, name="运营", customer_id=None)
    contact = await add_contact(session, customer, shared, name="张三")

    updated = await update_contact(
        session,
        customer.id,
        contact.id,
        ContactUpdate(function_id=other_shared.id),
        context=context,
    )

    assert updated.function_id == other_shared.id


async def test_update_to_foreign_function_returns_7010(session) -> None:
    user, context, customer, shared = await _setup(session)
    other = await make_customer_row(session, owner=user)
    foreign = await add_function(session, name="外部", customer_id=other.id)
    contact = await add_contact(session, customer, shared, name="张三")

    with pytest.raises(BusinessError) as exc:
        await update_contact(
            session,
            customer.id,
            contact.id,
            ContactUpdate(function_id=foreign.id),
            context=context,
        )

    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_contact_of_other_customer_returns_7005(session) -> None:
    user, context, customer, shared = await _setup(session)
    other = await make_customer_row(session, owner=user)
    contact = await add_contact(session, other, shared, name="别人的")

    with pytest.raises(BusinessError) as exc:
        await update_contact(
            session,
            customer.id,
            contact.id,
            ContactUpdate(name="改"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_deleted_contact_update_returns_7005(session) -> None:
    _, context, customer, shared = await _setup(session)
    contact = await add_contact(
        session,
        customer,
        shared,
        name="张三",
        status=ContactStatus.DELETED,
    )

    with pytest.raises(BusinessError) as exc:
        await update_contact(
            session,
            customer.id,
            contact.id,
            ContactUpdate(name="改"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_delete_contact_is_soft_and_hidden_from_list(session) -> None:
    _, context, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")

    await delete_contact(session, customer.id, contact.id, context=context)
    await session.refresh(contact)
    items, total = await list_contacts(
        session,
        customer.id,
        context=context,
        page=1,
        page_size=20,
    )

    assert contact.status == ContactStatus.DELETED
    assert contact.deleted_at is not None
    assert contact.deleted_by is not None
    assert items == []
    assert total == 0


async def test_restore_contact_reactivates(session) -> None:
    _, context, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")
    await delete_contact(session, customer.id, contact.id, context=context)

    restored = await restore_contact(
        session,
        customer.id,
        contact.id,
        context=context,
    )

    assert restored.status == ContactStatus.ACTIVE
    assert restored.deleted_at is None
    assert restored.deleted_by is None


async def test_restore_active_contact_returns_7012(session) -> None:
    _, context, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")

    with pytest.raises(BusinessError) as exc:
        await restore_contact(session, customer.id, contact.id, context=context)

    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_restore_contact_under_deleted_customer_returns_7001(
    session,
) -> None:
    _, context, customer, shared = await _setup(session)
    contact = await add_contact(
        session,
        customer,
        shared,
        name="张三",
        status=ContactStatus.DELETED,
    )
    customer.status = ContactStatus.DELETED
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await restore_contact(session, customer.id, contact.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_list_hidden_customer_returns_7001(session) -> None:
    user, _, customer, _ = await _setup(session)
    outsider = await make_user(session)
    outsider_context = make_context(
        outsider,
        permission_codes=PERMS,
        data_scope=DataScope.SELF,
    )

    with pytest.raises(BusinessError) as exc:
        await list_contacts(
            session,
            customer.id,
            context=outsider_context,
            page=1,
            page_size=20,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_list_is_paginated(session) -> None:
    _, context, customer, shared = await _setup(session)
    for index in range(5):
        await add_contact(session, customer, shared, name=f"人{index}")

    items, total = await list_contacts(
        session,
        customer.id,
        context=context,
        page=2,
        page_size=2,
    )

    assert total == 5
    assert len(items) == 2
    assert all(contact.customer_id == customer.id for contact, _ in items)


async def test_name_length_boundary(session) -> None:
    _, context, customer, shared = await _setup(session)

    created = await create_contact(
        session,
        customer.id,
        ContactCreate(name="x" * 255, function_id=shared.id),
        context=context,
    )

    assert len(created.name) == 255
    with pytest.raises(ValidationError):
        ContactCreate(name="x" * 256, function_id=shared.id)


async def test_name_over_length_is_also_rejected_by_service(session) -> None:
    _, context, customer, shared = await _setup(session)
    payload = ContactCreate.model_construct(
        name="x" * 256,
        function_id=shared.id,
    )

    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, payload, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD
