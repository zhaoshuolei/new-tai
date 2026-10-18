"""跟进方式服务测试：唯一性、引用校验、删除与恢复。"""

import pytest

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    make_context,
    make_customer_row,
    make_user,
    session,
)
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


async def test_non_super_admin_cannot_write_methods(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_method(session, "钉钉", context=ctx)
    assert error.value.code == ErrorCode.FORBIDDEN

    methods = await follow_up_service.list_methods(session, context=ctx)
    assert methods == []


async def test_non_super_admin_cannot_read_recycle_bin(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.list_deleted_methods(session, context=ctx)
    assert error.value.code == ErrorCode.FORBIDDEN


async def test_create_method_without_name_returns_7003(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)

    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_method(session, None, context=ctx)
    assert error.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_update_method_without_name_returns_7003(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_method(session, method.id, None, context=ctx)
    assert error.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_method_name_must_be_unique_among_active(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    await follow_up_service.create_method(session, "电话", context=ctx)

    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_method(session, "  电话  ", context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS


async def test_deleted_method_frees_its_name(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    created = await follow_up_service.create_method(session, "电话", context=ctx)
    await follow_up_service.delete_method(session, created.id, context=ctx)

    recreated = await follow_up_service.create_method(session, "电话", context=ctx)
    assert recreated.id != created.id


async def test_update_method_renames_and_rejects_duplicate(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    await follow_up_service.create_method(session, "电话", context=ctx)
    method = await follow_up_service.create_method(session, "微信", context=ctx)

    renamed = await follow_up_service.update_method(
        session,
        method.id,
        "拜访",
        context=ctx,
    )
    assert renamed.name == "拜访"

    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_method(
            session,
            method.id,
            "电话",
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS


async def test_update_missing_method_returns_7014(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_method(
            session,
            method.id + 1_000_000,
            "微信",
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND


async def test_delete_missing_method_returns_7014(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.delete_method(
            session,
            method.id + 1_000_000,
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND


async def test_method_in_use_cannot_be_deleted(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    await add_follow_up(session, contact, method, follow_up_user=user)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=user,
        status=ContactStatus.DELETED,
    )

    with pytest.raises(BusinessError) as error:
        await follow_up_service.delete_method(session, method.id, context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_IN_USE


async def test_unused_method_can_be_deleted_and_restored(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await follow_up_service.create_method(session, "电话", context=ctx)

    await follow_up_service.delete_method(session, method.id, context=ctx)
    assert method.status == ContactStatus.DELETED

    restored = await follow_up_service.restore_method(session, method.id, context=ctx)
    assert restored.status == ContactStatus.ACTIVE


async def test_restore_conflicts_with_active_name(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    deleted = await add_method(session, name="电话", status=ContactStatus.DELETED)
    await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_method(session, deleted.id, context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS


async def test_restore_active_method_returns_7012(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_method(session, method.id, context=ctx)
    assert error.value.code == ErrorCode.RECORD_NOT_DELETED
