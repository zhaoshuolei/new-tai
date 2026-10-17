"""冒烟测试：确认共享 helper 能在真实数据库上构造数据并通过 HTTP 访问。"""

from sqlalchemy.ext.asyncio import AsyncSession

from tests.customer_helpers import (
    FakeTianyanchaClient,
    api_client,
    make_context,
    make_customer_row,
    make_department,
    make_user,
    session,
)

__all__ = ("session",)


async def test_helpers_build_user_customer_and_context(session: AsyncSession) -> None:
    department = await make_department(session)
    owner = await make_user(session, department=department)
    customer = await make_customer_row(session, owner=owner)
    context = make_context(owner, permission_codes={"customer:read"})

    assert customer.owner_user_id == owner.id
    assert customer.owner_department_id == department.id
    assert context.permission_codes == frozenset({"customer:read"})
    assert context.is_super_admin is False


async def test_api_client_reaches_customer_detail(session: AsyncSession) -> None:
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    context = make_context(owner, permission_codes={"customer:read"})

    async with api_client(session, context) as client:
        response = await client.get(f"/api/v1/customers/{customer.id}")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == customer.id


def test_fake_tianyancha_client_defaults() -> None:
    client = FakeTianyanchaClient()
    assert client.calls == []
    assert "经营范围" in client.historical_text
