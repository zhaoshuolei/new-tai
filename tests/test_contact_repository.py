from app.models.contact import ContactStatus
from app.repositories import contact as contact_repository
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_customer_row, make_user, session

__all__ = ("session",)


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
