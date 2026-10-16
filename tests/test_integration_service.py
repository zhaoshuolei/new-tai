"""集成配置服务（天眼查 token 管理）测试。

只做离线验证：不发起任何真实网络调用，也不使用真实 token。
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clients.tianyancha import TianyanchaClient
from app.core.auth_context import AuthContext
from app.core.crypto import decrypt_secret
from app.core.errors import BusinessError, ErrorCode
from app.repositories.integration_config import get_by_provider
from app.services.integration import (
    get_tianyancha_client,
    get_tianyancha_status,
    save_tianyancha_token,
)
from app.services.integration import test_tianyancha_connection as probe_connection

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


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


@pytest.fixture
def context() -> AuthContext:
    return AuthContext(
        user_id=42,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(),
        is_super_admin=False,
    )


async def test_status_reports_not_configured(session: AsyncSession) -> None:
    status = await get_tianyancha_status(session)
    assert status == {"configured": False, "updated_at": None, "updated_by": None}


async def test_save_then_status(session: AsyncSession, context: AuthContext) -> None:
    await save_tianyancha_token(session, "mcpk2_secret_value", context=context)

    status = await get_tianyancha_status(session)
    assert status["configured"] is True
    assert status["updated_by"] == context.user_id
    assert isinstance(status["updated_at"], datetime)
    assert "mcpk2_secret_value" not in str(status)

    row = await get_by_provider(session, "tianyancha")
    assert row is not None
    assert row.secret_ciphertext != "mcpk2_secret_value"
    assert decrypt_secret(row.secret_ciphertext) == "mcpk2_secret_value"


@pytest.mark.parametrize("api_key", ["", "   "])
async def test_save_rejects_empty(
    session: AsyncSession, context: AuthContext, api_key: str
) -> None:
    with pytest.raises(BusinessError) as exc_info:
        await save_tianyancha_token(session, api_key, context=context)
    assert exc_info.value.code == ErrorCode.INVALID_PARAMETER
    assert exc_info.value.http_status == 422
    assert await get_by_provider(session, "tianyancha") is None


async def test_save_strips_surrounding_whitespace(
    session: AsyncSession, context: AuthContext
) -> None:
    await save_tianyancha_token(session, "  mcpk2_trimmed  ", context=context)
    row = await get_by_provider(session, "tianyancha")
    assert row is not None
    assert decrypt_secret(row.secret_ciphertext) == "mcpk2_trimmed"


async def test_client_raises_not_configured(session: AsyncSession) -> None:
    with pytest.raises(BusinessError) as exc_info:
        await get_tianyancha_client(session)
    assert exc_info.value.code == ErrorCode.TIANYANCHA_NOT_CONFIGURED
    assert exc_info.value.http_status == 409


async def test_client_returns_configured_client(
    session: AsyncSession, context: AuthContext
) -> None:
    await save_tianyancha_token(session, "mcpk2_client_value", context=context)

    client = await get_tianyancha_client(session)
    assert isinstance(client, TianyanchaClient)
    assert client._api_key == "mcpk2_client_value"


async def test_client_logs_decrypt_failure_without_ciphertext(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    foreign_key = Fernet.generate_key()
    ciphertext = Fernet(foreign_key).encrypt(b"unreadable-value").decode("utf-8")
    from app.models.integration_config import IntegrationConfig

    session.add(
        IntegrationConfig(
            provider="tianyancha",
            secret_ciphertext=ciphertext,
            updated_by=1,
        )
    )
    await session.flush()

    with caplog.at_level("ERROR"), pytest.raises(BusinessError) as exc_info:
        await get_tianyancha_client(session)

    assert exc_info.value.code == ErrorCode.TIANYANCHA_TOKEN_UNREADABLE
    assert exc_info.value.http_status == 503
    assert any(record.levelno >= 40 for record in caplog.records)
    assert ciphertext not in caplog.text
    assert "unreadable-value" not in caplog.text


async def test_connection_success_is_awaited(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await save_tianyancha_token(session, "mcpk2_probe_value", context=context)
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(TianyanchaClient, "test_connection", mock)

    result = await probe_connection(session, context=context)

    assert result is None
    assert mock.await_count == 1


async def test_connection_propagates_failure(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await save_tianyancha_token(session, "mcpk2_probe_value", context=context)

    async def _boom(self: TianyanchaClient) -> None:
        raise BusinessError(ErrorCode.TIANYANCHA_AUTH_FAILED, "auth failed", 502)

    monkeypatch.setattr(TianyanchaClient, "test_connection", _boom)

    with pytest.raises(BusinessError) as exc_info:
        await probe_connection(session, context=context)
    assert exc_info.value.code == ErrorCode.TIANYANCHA_AUTH_FAILED
