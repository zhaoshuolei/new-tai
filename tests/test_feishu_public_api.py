"""飞书授权引导公开接口的免登录契约与安全边界测试。"""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.errors import ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.integration_config import IntegrationConfig
from tests.feishu_helpers import add_feishu_config

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
PUBLIC_FEISHU_PATH = "/api/v1/integrations/feishu/public"
ENVIRONMENT_REDIRECT_URI = "https://login.example.com/auth/feishu/callback"
DATABASE_REDIRECT_URI = "https://admin.example.com/auth/feishu/callback"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            # 隔离其他测试遗留的飞书配置；事务回滚后不影响测试库。
            await test_session.execute(
                delete(IntegrationConfig).where(IntegrationConfig.provider == "feishu")
            )
            await test_session.flush()
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture(autouse=True)
def unconfigured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    monkeypatch.setattr(settings, "feishu_redirect_uri", "")


@pytest_asyncio.fixture
async def public_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """构造不注入 AuthContext、不携带 Authorization 的匿名客户端。"""
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


async def test_public_endpoint_allows_anonymous_requests(
    public_client: AsyncClient,
) -> None:
    response = await public_client.get(PUBLIC_FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert "data" in response.json()


async def test_unconfigured_environment_reports_not_configured(
    public_client: AsyncClient,
) -> None:
    response = await public_client.get(PUBLIC_FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": False,
        "app_id": None,
        "redirect_uri": None,
    }


async def test_environment_configuration_is_exposed_without_secret(
    public_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "env-secret-must-not-leak"
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env-public")
    monkeypatch.setattr(settings, "feishu_app_secret", secret)
    monkeypatch.setattr(settings, "feishu_redirect_uri", ENVIRONMENT_REDIRECT_URI)

    response = await public_client.get(PUBLIC_FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": True,
        "app_id": "cli-env-public",
        "redirect_uri": ENVIRONMENT_REDIRECT_URI,
    }
    assert secret not in response.text
    assert "app_secret" not in response.text


async def test_database_configuration_takes_precedence(
    public_client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env-shadowed")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret-shadowed")
    monkeypatch.setattr(settings, "feishu_redirect_uri", ENVIRONMENT_REDIRECT_URI)
    secret = "db-secret-must-not-leak"
    await add_feishu_config(
        session,
        app_id="cli-db-public",
        app_secret=secret,
        redirect_uri=DATABASE_REDIRECT_URI,
    )

    response = await public_client.get(PUBLIC_FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": True,
        "app_id": "cli-db-public",
        "redirect_uri": DATABASE_REDIRECT_URI,
    }
    assert secret not in response.text
    assert "app_secret" not in response.text


async def test_response_data_has_exactly_three_keys(
    public_client: AsyncClient,
    session: AsyncSession,
) -> None:
    expected_keys = {"configured", "app_id", "redirect_uri"}

    unconfigured = await public_client.get(PUBLIC_FEISHU_PATH)
    assert set(unconfigured.json()["data"]) == expected_keys

    await add_feishu_config(session)

    configured = await public_client.get(PUBLIC_FEISHU_PATH)
    assert set(configured.json()["data"]) == expected_keys


async def test_damaged_database_configuration_fails_like_admin_api(
    public_client: AsyncClient,
    session: AsyncSession,
) -> None:
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()

    response = await public_client.get(PUBLIC_FEISHU_PATH)

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.FEISHU_CONFIG_UNREADABLE
