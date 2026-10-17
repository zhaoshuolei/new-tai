"""飞书集成配置 API 契约与权限测试。"""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.clients.feishu import FeishuClient
from app.core import exception_handlers
from app.core.audit_meta import get_audit_route_meta
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.integration_config import IntegrationConfig
from app.services import audit as audit_service
from tests.feishu_helpers import add_feishu_config

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
FEISHU_PATH = "/api/v1/integrations/feishu"
FEISHU_TEST_PATH = "/api/v1/integrations/feishu/test"
ApiClientFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]


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
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest.fixture
def api_client_factory(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> ApiClientFactory:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    monkeypatch.setattr(settings, "feishu_redirect_uri", "")

    @asynccontextmanager
    async def factory(
        *,
        permissions: tuple[str, ...],
    ) -> AsyncIterator[AsyncClient]:
        application = create_app()
        context = AuthContext(
            user_id=42,
            role_id=None,
            department_id=None,
            data_scope=None,
            custom_department_ids=frozenset(),
            include_children=False,
            permission_codes=frozenset(permissions),
            is_super_admin=False,
            username="api-tester",
        )

        async def override_session() -> AsyncIterator[AsyncSession]:
            yield session

        async def override_context() -> AuthContext:
            return context

        application.dependency_overrides[get_session] = override_session
        application.dependency_overrides[get_current_context] = override_context
        try:
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
            ) as client:
                yield client
        finally:
            application.dependency_overrides.clear()

    return factory


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as audit_session:
        rows = await audit_session.scalars(select(AuditLog).order_by(AuditLog.id))
        return list(rows.all())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", FEISHU_PATH),
        ("put", FEISHU_PATH),
        ("post", FEISHU_TEST_PATH),
        ("delete", FEISHU_PATH),
    ],
)
async def test_feishu_config_requires_permissions(
    api_client_factory: ApiClientFactory,
    method: str,
    path: str,
) -> None:
    async with api_client_factory(permissions=()) as client:
        response = await client.request(
            method.upper(),
            path,
            json={"app_id": "cli-test", "app_secret": "secret"}
            if method == "put"
            else None,
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", FEISHU_PATH),
        ("post", FEISHU_TEST_PATH),
        ("delete", FEISHU_PATH),
    ],
)
async def test_read_permission_cannot_mutate_or_probe(
    api_client_factory: ApiClientFactory,
    method: str,
    path: str,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.request(
            method.upper(),
            path,
            json={"app_id": "cli-test", "app_secret": "secret"}
            if method == "put"
            else None,
        )
    assert response.status_code == 403


async def test_get_returns_source_without_secret(
    api_client_factory: ApiClientFactory,
    session: AsyncSession,
) -> None:
    await add_feishu_config(session, app_secret="must-not-leak")
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get(FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["message"] == "ok"
    data = response.json()["data"]
    assert data["source"] == "database"
    assert data["app_id"] == "cli-test"
    assert "must-not-leak" not in response.text
    assert "app_secret" not in response.text


async def test_put_saves_and_returns_database_status(
    api_client_factory: ApiClientFactory,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            FEISHU_PATH,
            json={
                "app_id": "cli-api",
                "app_secret": "api-secret",
                "redirect_uri": "https://admin.example.com/callback",
            },
        )

    assert response.status_code == 200
    assert response.json()["message"] == "updated"
    assert response.json()["data"]["source"] == "database"
    assert response.json()["data"]["app_id"] == "cli-api"
    assert "api-secret" not in response.text


async def test_get_reports_none_source(
    api_client_factory: ApiClientFactory,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get(FEISHU_PATH)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": False,
        "source": "none",
        "app_id": None,
        "redirect_uri": None,
        "updated_at": None,
        "updated_by": None,
    }


async def test_first_save_without_secret_returns_1006(
    api_client_factory: ApiClientFactory,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            FEISHU_PATH,
            json={"app_id": "cli-missing-secret"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.INVALID_PARAMETER


async def test_test_and_delete_require_effective_config(
    api_client_factory: ApiClientFactory,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        missing = await client.post(FEISHU_TEST_PATH)
    assert missing.status_code == 409
    assert missing.json()["code"] == ErrorCode.FEISHU_NOT_CONFIGURED

    await add_feishu_config(session)
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        tested = await client.post(FEISHU_TEST_PATH)
        deleted = await client.delete(FEISHU_PATH)
    assert tested.status_code == 200
    assert tested.json()["message"] == "ok"
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "deleted"
    assert deleted.json()["data"]["source"] == "none"


async def test_get_maps_damaged_config_to_8010(
    api_client_factory: ApiClientFactory,
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

    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get(FEISHU_PATH)

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.FEISHU_CONFIG_UNREADABLE


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_feishu_write_routes_have_audit_metadata(method: str) -> None:
    meta = get_audit_route_meta(method, "/integrations/feishu")

    assert meta is not None
    assert meta.action == "integration_feishu:update"
    assert meta.resource_type == "integration"


async def test_put_success_records_audit_without_secret(
    api_client_factory: ApiClientFactory,
    audit_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            FEISHU_PATH,
            json={"app_id": "cli-audit", "app_secret": "audit-secret"},
        )

    assert response.status_code == 200
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].action == "integration_feishu:update"
    assert rows[0].resource_type == "integration"
    assert rows[0].result is AuditResult.SUCCESS
    assert "audit-secret" not in repr(rows[0].__dict__)


async def test_delete_is_audited_even_when_idempotent(
    api_client_factory: ApiClientFactory,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        first = await client.delete(FEISHU_PATH)
        second = await client.delete(FEISHU_PATH)

    assert first.status_code == 200
    assert second.status_code == 200
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 2
    assert all(row.result is AuditResult.SUCCESS for row in rows)


async def test_permission_denial_is_audited_but_test_probe_is_not(
    api_client_factory: ApiClientFactory,
    audit_session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session)
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        denied = await client.put(
            FEISHU_PATH,
            json={"app_id": "cli-denied", "app_secret": "denied-secret"},
        )
    assert denied.status_code == 403

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        tested = await client.post(FEISHU_TEST_PATH)
    assert tested.status_code == 200

    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].reason == "FORBIDDEN"
    assert "denied-secret" not in repr(rows[0].__dict__)


async def test_save_errors_and_logs_never_contain_secret(
    api_client_factory: ApiClientFactory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "never-log-this-app-secret"

    async def reject(_self: FeishuClient) -> None:
        raise BusinessError(
            ErrorCode.FEISHU_CREDENTIALS_REJECTED,
            "Feishu credentials rejected",
            http_status=422,
        )

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", reject)
    with caplog.at_level("DEBUG"):
        async with api_client_factory(
            permissions=("integration_feishu:update",)
        ) as client:
            response = await client.put(
                FEISHU_PATH,
                json={"app_id": "cli-bad", "app_secret": secret},
            )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.FEISHU_CREDENTIALS_REJECTED
    assert secret not in response.text
    assert secret not in caplog.text


async def test_put_maps_upstream_failure_to_1011(
    api_client_factory: ApiClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "upstream-secret"

    async def fail(_self: FeishuClient) -> None:
        raise BusinessError(
            ErrorCode.FEISHU_UPSTREAM_ERROR,
            "Feishu upstream request failed",
            http_status=502,
        )

    record_exception = AsyncMock(return_value=None)
    monkeypatch.setattr(FeishuClient, "verify_app_credentials", fail)
    monkeypatch.setattr(exception_handlers, "record_exception", record_exception)

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            FEISHU_PATH,
            json={"app_id": "cli-upstream", "app_secret": secret},
        )

    assert response.status_code == 502
    assert response.json()["code"] == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert secret not in response.text
    record_exception.assert_awaited_once()


async def test_test_probe_is_not_audited(
    api_client_factory: ApiClientFactory,
    audit_session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session)
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.post(FEISHU_TEST_PATH)

    assert response.status_code == 200
    async with audit_session_factory() as audit_session:
        rows = await audit_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "integration_feishu:update",
            )
        )
    assert list(rows.all()) == []
