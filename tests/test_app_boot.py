import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_app_boots_and_serves_openapi() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "tianxiadmin"


def test_openapi_declares_business_domain_tags() -> None:
    tag_names = {tag["name"] for tag in app.openapi()["tags"]}

    assert tag_names == {"认证", "用户", "角色", "菜单", "部门", "健康"}


@pytest.mark.asyncio
async def test_app_lifespan_initializes_and_releases_clients() -> None:
    async with app.router.lifespan_context(app):
        assert app.state.redis_client is not None
        assert app.state.db_engine is not None

    assert app.state.redis_client is None
    assert app.state.db_engine is None
