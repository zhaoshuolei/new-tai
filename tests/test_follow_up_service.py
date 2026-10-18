from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.role import DataScope
from app.schemas.follow_up import FollowUpCreate, FollowUpUpdate
from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


async def _fixture(session):  # noqa: F811
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    return owner, customer, contact, method, ctx


async def test_create_defaults_follow_up_user_to_caller(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="电话沟通预算"),
        context=ctx,
    )
    assert created.id == created.follow_up.id
    assert created.follow_up.follow_up_user_id == owner.id
    assert created.follow_up.created_by == owner.id
    assert created.method_name == "电话"


async def test_create_rejects_unavailable_follow_up_user(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    invisible = await make_user(session)
    # 用 SELF 范围调用：只能看到自己名下的客户，指定他人作为跟进人 → 不可见 → 7021
    hidden = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        data_scope=DataScope.SELF,
    )
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            customer.id,
            contact.id,
            FollowUpCreate(
                method_id=method.id,
                content="x",
                follow_up_user_id=invisible.id,
            ),
            context=hidden,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_USER_UNAVAILABLE


async def test_create_validates_content_and_times(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    now = datetime.now(UTC).replace(tzinfo=None)
    cases = [
        {"content": "   "},
        {"content": "x" * 5001},
        {"content": "ok", "follow_up_at": now + timedelta(minutes=30)},
        {
            "content": "ok",
            "follow_up_at": now,
            "next_follow_up_at": now - timedelta(minutes=1),
        },
    ]
    for fields in cases:
        with pytest.raises(BusinessError) as error:
            await follow_up_service.create_follow_up(
                session,
                customer.id,
                contact.id,
                FollowUpCreate(method_id=method.id, **fields),
                context=ctx,
            )
        assert error.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_create_rejects_deleted_method(session) -> None:  # noqa: F811
    owner, customer, contact, _, ctx = await _fixture(session)
    deleted = await add_method(session, name="旧方式", status=ContactStatus.DELETED)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            customer.id,
            contact.id,
            FollowUpCreate(method_id=deleted.id, content="x"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND


async def test_foreign_customer_contact_pair_returns_7005(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    other_customer = await make_customer_row(session, owner=owner)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            other_customer.id,
            contact.id,
            FollowUpCreate(method_id=method.id, content="x"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_only_owner_can_update_and_delete(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    other = await make_user(session)
    decoy = await make_user(session)
    created = await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=other,
        content="别人的记录",
    )
    ctx = make_context(
        decoy,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="改一下"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    with pytest.raises(BusinessError) as error:
        await follow_up_service.delete_follow_up(
            session, customer.id, contact.id, created.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_super_admin_cannot_edit_others_record(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    created = await add_follow_up(
        session, contact, method, follow_up_user=author, content="原始"
    )
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        is_super_admin=True,
    )
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="超管改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_transfer_moves_edit_rights(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    colleague = await make_user(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="原始"),
        context=ctx,
    )
    await follow_up_service.update_follow_up(
        session,
        customer.id,
        contact.id,
        created.follow_up.id,
        FollowUpUpdate(follow_up_user_id=colleague.id),
        context=ctx,
    )

    # 原跟进人（= owner）已不再拥有编辑权
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.follow_up.id,
            FollowUpUpdate(content="再改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    colleague_ctx = make_context(
        colleague,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    updated = await follow_up_service.update_follow_up(
        session,
        customer.id,
        contact.id,
        created.follow_up.id,
        FollowUpUpdate(content="新跟进人改的"),
        context=colleague_ctx,
    )
    assert updated.follow_up.content == "新跟进人改的"


async def test_recycle_bin_lists_only_own_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    mine = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="我的"),
        context=ctx,
    )
    await follow_up_service.delete_follow_up(
        session, customer.id, contact.id, mine.follow_up.id, context=ctx
    )
    other = await make_user(session)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=other,
        status=ContactStatus.DELETED,
    )

    rows, total = await follow_up_service.list_deleted_follow_ups(
        session, customer.id, contact.id, context=ctx, page=1, page_size=20
    )
    assert total == 1
    assert rows[0].follow_up.id == mine.follow_up.id


async def test_aware_times_are_normalized_to_naive_utc(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(
            method_id=method.id,
            content="带时区",
            follow_up_at=datetime(2026, 10, 1, 2, 0, tzinfo=UTC),
            next_follow_up_at=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
        ),
        context=ctx,
    )
    assert created.follow_up.follow_up_at == datetime(2026, 10, 1, 2, 0)
    assert created.follow_up.follow_up_at.tzinfo is None
    assert created.follow_up.next_follow_up_at == datetime(2026, 10, 1, 3, 0)

    updated = await follow_up_service.update_follow_up(
        session,
        customer.id,
        contact.id,
        created.id,
        FollowUpUpdate(
            follow_up_at=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
            next_follow_up_at=datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        ),
        context=ctx,
    )
    assert updated.follow_up.follow_up_at == datetime(2026, 10, 1, 4, 0)
    assert updated.follow_up.follow_up_at.tzinfo is None
    assert updated.follow_up.next_follow_up_at == datetime(2026, 10, 1, 5, 0)


async def test_restore_requires_deleted_record_and_current_owner(  # noqa: F811
    session,
) -> None:
    owner, customer, contact, method, ctx = await _fixture(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="待恢复"),
        context=ctx,
    )

    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            context=ctx,
        )
    assert error.value.code == ErrorCode.RECORD_NOT_DELETED

    await follow_up_service.delete_follow_up(
        session,
        customer.id,
        contact.id,
        created.id,
        context=ctx,
    )
    other = await make_user(session)
    other_ctx = make_context(other)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            context=other_ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    restored = await follow_up_service.restore_follow_up(
        session,
        customer.id,
        contact.id,
        created.id,
        context=ctx,
    )
    assert restored.follow_up.status == ContactStatus.ACTIVE
    assert restored.follow_up.deleted_at is None
    assert restored.follow_up.deleted_by is None


async def test_deleted_contact_hides_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    await add_follow_up(session, contact, method, follow_up_user=owner)
    contact.status = ContactStatus.DELETED
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await follow_up_service.list_follow_ups(
            session, customer.id, contact.id, context=ctx, page=1, page_size=20
        )
    assert error.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_record_of_soft_deleted_user_stays_readable_but_read_only(  # noqa: F811
    session,
) -> None:
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    created = await add_follow_up(
        session, contact, method, follow_up_user=author, content="离职前写的"
    )
    author.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )

    rows, total = await follow_up_service.list_follow_ups(
        session, customer.id, contact.id, context=ctx, page=1, page_size=20
    )
    assert total == 1

    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="想改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER
