from sqlalchemy import select

from app.models.contact import ContactFunction, ContactStatus
from scripts.init_data import _ensure_shared_contact_functions
from tests.customer_helpers import session  # noqa: F401


async def test_shared_functions_are_seeded_once(session) -> None:  # noqa: F811
    await _ensure_shared_contact_functions(session)
    await _ensure_shared_contact_functions(session)

    rows = (
        await session.scalars(
            select(ContactFunction).where(ContactFunction.customer_id.is_(None))
        )
    ).all()

    assert sorted(row.name for row in rows) == ["产品", "商务", "研发", "运营"]
    assert all(row.scope_key == 0 for row in rows)
    assert all(row.customer_id is None for row in rows)
    assert all(row.status == ContactStatus.ACTIVE for row in rows)


async def test_existing_active_shared_function_is_not_duplicated(
    session,  # noqa: F811
) -> None:
    session.add(
        ContactFunction(
            name="商务",
            customer_id=None,
            scope_key=0,
            status=ContactStatus.ACTIVE,
        )
    )
    await session.flush()

    await _ensure_shared_contact_functions(session)

    rows = (
        await session.scalars(
            select(ContactFunction).where(ContactFunction.customer_id.is_(None))
        )
    ).all()
    business_rows = [row for row in rows if row.name == "商务"]

    assert len(business_rows) == 1
    assert len(rows) == 4
    assert business_rows[0].status == ContactStatus.ACTIVE


async def test_deleted_shared_function_does_not_block_active_seed(
    session,  # noqa: F811
) -> None:
    session.add(
        ContactFunction(
            name="商务",
            customer_id=None,
            scope_key=0,
            status=ContactStatus.DELETED,
        )
    )
    await session.flush()

    await _ensure_shared_contact_functions(session)

    rows = (
        await session.scalars(
            select(ContactFunction).where(ContactFunction.customer_id.is_(None))
        )
    ).all()
    business_rows = [row for row in rows if row.name == "商务"]
    active_rows = [row for row in business_rows if row.status == ContactStatus.ACTIVE]
    deleted_rows = [row for row in business_rows if row.status == ContactStatus.DELETED]

    assert len(rows) == 5
    assert len(business_rows) == 2
    assert len(active_rows) == 1
    assert len(deleted_rows) == 1
    assert active_rows[0].scope_key == 0
    assert active_rows[0].customer_id is None
    assert active_rows[0].active_name == "商务"
