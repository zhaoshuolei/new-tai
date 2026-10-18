"""跟进方式与跟进记录仓储层测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.contact import ContactStatus
from app.models.follow_up import ContactFollowUp, ContactFollowUpAttachment
from app.repositories import follow_up as follow_up_repository
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_attachment, add_follow_up, add_method

__all__ = ("session",)


async def test_list_methods_filters_status_and_sorts(session) -> None:  # noqa: F811
    await add_method(session, name="微信")
    await add_method(session, name="电话")
    await add_method(session, name="已删", status=ContactStatus.DELETED)

    methods = await follow_up_repository.list_methods(
        session, status=ContactStatus.ACTIVE
    )

    assert [method.name for method in methods] == ["微信", "电话"]


@pytest.mark.parametrize(
    "lock_method",
    [
        follow_up_repository.lock_method_for_update,
        follow_up_repository.lock_method_shared,
    ],
)
async def test_get_and_lock_method(session, lock_method) -> None:  # noqa: F811
    method = await add_method(session, name="拜访")

    loaded = await follow_up_repository.get_method(session, method.id)
    locked = await lock_method(session, method.id)

    assert loaded is not None and loaded.id == method.id
    assert locked is not None and locked.id == method.id
    assert await follow_up_repository.get_method(session, -1) is None
    assert await lock_method(session, -1) is None


async def test_list_follow_ups_orders_filters_and_pages(session) -> None:  # noqa: F811
    user = await make_user(session)
    user.nickname = "跟进人"
    creator = await make_user(session)
    creator.nickname = "录入人"
    other_user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    base = datetime(2026, 10, 1, tzinfo=UTC)
    records = []
    for index in range(3):
        records.append(
            await add_follow_up(
                session,
                contact,
                method,
                follow_up_user=user,
                content=f"第{index}条",
                follow_up_at=base + timedelta(days=index),
            )
        )
    records[-1].created_by = creator.id
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=other_user,
        content="他人跟进",
        follow_up_at=base + timedelta(days=10),
    )
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=user,
        content="已删除",
        follow_up_at=base + timedelta(days=11),
        status=ContactStatus.DELETED,
    )

    rows, total = await follow_up_repository.list_follow_ups(
        session,
        contact.id,
        status=ContactStatus.ACTIVE,
        follow_up_user_id=user.id,
        page=1,
        page_size=2,
    )
    second_page, second_total = await follow_up_repository.list_follow_ups(
        session,
        contact.id,
        status=ContactStatus.ACTIVE,
        follow_up_user_id=user.id,
        page=2,
        page_size=2,
    )
    deleted_rows, deleted_total = await follow_up_repository.list_follow_ups(
        session,
        contact.id,
        status=ContactStatus.DELETED,
        follow_up_user_id=None,
        page=1,
        page_size=10,
    )

    assert total == second_total == 3
    assert [row.follow_up.content for row in rows] == ["第2条", "第1条"]
    assert [row.follow_up.content for row in second_page] == ["第0条"]
    assert rows[0].contact_name == contact.name
    assert rows[0].method_name == "电话"
    assert rows[0].follow_up_user_nickname == "跟进人"
    assert rows[0].follow_up_user_username == user.username
    assert rows[0].created_by_nickname == "录入人"
    assert rows[0].created_by_username == creator.username
    assert deleted_total == 1
    assert [row.follow_up.content for row in deleted_rows] == ["已删除"]


async def test_get_and_lock_follow_up_are_scoped_to_contact(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    other_contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = await add_follow_up(session, contact, method, follow_up_user=user)

    loaded = await follow_up_repository.get_follow_up(session, contact.id, record.id)
    locked = await follow_up_repository.lock_follow_up_for_update(
        session, contact.id, record.id
    )

    assert loaded is not None and loaded.id == record.id
    assert locked is not None and locked.id == record.id
    assert (
        await follow_up_repository.get_follow_up(session, other_contact.id, record.id)
        is None
    )
    assert (
        await follow_up_repository.lock_follow_up_for_update(
            session, other_contact.id, record.id
        )
        is None
    )


async def test_get_follow_up_row_returns_display_fields(session) -> None:  # noqa: F811
    user = await make_user(session)
    user.nickname = "跟进人"
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = await add_follow_up(session, contact, method, follow_up_user=user)

    row = await follow_up_repository.get_follow_up_row(session, record.id)

    assert row is not None
    assert row.follow_up.id == record.id
    assert row.contact_name == contact.name
    assert row.method_name == "电话"
    assert row.follow_up_user_nickname == "跟进人"
    assert row.follow_up_user_username == user.username
    assert row.created_by_nickname == "跟进人"
    assert row.created_by_username == user.username
    assert await follow_up_repository.get_follow_up_row(session, -1) is None


async def test_reference_ids_include_deleted_records(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    active = await add_follow_up(session, contact, method, follow_up_user=user)
    deleted = await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=user,
        status=ContactStatus.DELETED,
    )

    ids = await follow_up_repository.lock_follow_up_ids_referencing_method(
        session, method.id
    )

    assert ids == [active.id, deleted.id]


async def test_add_follow_up_flushes_and_refreshes(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = ContactFollowUp(
        contact_id=contact.id,
        method_id=method.id,
        follow_up_user_id=user.id,
        follow_up_at=datetime(2026, 10, 1, 2, 0),
        content="仓储层写入",
        created_by=user.id,
    )

    created = await follow_up_repository.add_follow_up(session, record)

    assert created.id is not None
    assert created.created_at is not None
    assert created.content == "仓储层写入"


async def test_list_attachments_groups_by_record(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    first = await add_follow_up(session, contact, method, follow_up_user=user)
    second = await add_follow_up(session, contact, method, follow_up_user=user)
    first_attachment = await add_attachment(session, first, original_name="a.pdf")
    second_attachment = await add_attachment(session, first, original_name="c.pdf")
    third_attachment = await add_attachment(session, second, original_name="b.pdf")

    grouped = await follow_up_repository.list_attachments(
        session, [first.id, second.id]
    )

    assert [row.id for row in grouped[first.id]] == [
        first_attachment.id,
        second_attachment.id,
    ]
    assert [row.id for row in grouped[second.id]] == [third_attachment.id]
    assert [row.original_name for row in grouped[second.id]] == ["b.pdf"]
    assert grouped.get(999_999, []) == []
    assert await follow_up_repository.list_attachments(session, []) == {}


async def test_count_get_and_delete_attachments(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = await add_follow_up(session, contact, method, follow_up_user=user)
    first = await add_attachment(session, record)
    second = await add_attachment(session, record)

    assert await follow_up_repository.count_attachments(session, record.id) == 2
    loaded = await follow_up_repository.get_attachment(session, record.id, first.id)
    assert loaded is not None and loaded.id == first.id
    assert (
        await follow_up_repository.get_attachment(session, record.id, 999_999) is None
    )

    await follow_up_repository.delete_attachment_row(session, first)

    assert await follow_up_repository.count_attachments(session, record.id) == 1
    assert (
        await follow_up_repository.get_attachment(session, record.id, first.id) is None
    )
    assert (
        await follow_up_repository.get_attachment(session, record.id, second.id)
        is not None
    )


async def test_add_attachment_flushes_and_refreshes(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = await add_follow_up(session, contact, method, follow_up_user=user)
    attachment = ContactFollowUpAttachment(
        follow_up_id=record.id,
        storage_path=f"contact-follow-up/1/{record.id}.pdf",
        original_name="仓储层.pdf",
        content_type="application/pdf",
        size_bytes=1,
        created_by=user.id,
    )

    created = await follow_up_repository.add_attachment(session, attachment)

    assert created.id is not None
    assert created.created_at is not None
    assert created.original_name == "仓储层.pdf"
