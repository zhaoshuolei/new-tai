from sqlalchemy import select

from app.models.contact import ContactStatus
from app.models.follow_up import ContactFollowUpMethod
from scripts.init_data import _ensure_follow_up_methods
from tests.customer_helpers import session  # noqa: F401


async def test_follow_up_methods_are_seeded_once(session) -> None:  # noqa: F811
    await _ensure_follow_up_methods(session)
    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    assert sorted(row.name for row in rows) == ["微信", "拜访", "电话", "邮件"]
    assert all(row.status == ContactStatus.ACTIVE for row in rows)
    assert all(row.active_name == row.name for row in rows)


async def test_existing_active_method_is_not_duplicated(session) -> None:  # noqa: F811
    session.add(ContactFollowUpMethod(name="电话", status=ContactStatus.ACTIVE))
    await session.flush()

    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    assert len(rows) == 4
    assert len([row for row in rows if row.name == "电话"]) == 1


async def test_deleted_method_is_not_restored_by_seed(session) -> None:  # noqa: F811
    session.add(ContactFollowUpMethod(name="电话", status=ContactStatus.DELETED))
    await session.flush()

    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    method_rows = [row for row in rows if row.name == "电话"]
    assert len(method_rows) == 2
    assert len([row for row in method_rows if row.status == ContactStatus.ACTIVE]) == 1
    assert len([row for row in method_rows if row.status == ContactStatus.DELETED]) == 1
    assert len([row for row in rows if row.status == ContactStatus.ACTIVE]) == 4
