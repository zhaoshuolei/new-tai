"""Feishu integration configuration schema and service tests."""

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clients.feishu import FeishuClient
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import BusinessError, ErrorCode
from app.models.integration_config import IntegrationConfig
from app.repositories.integration_config import get_by_provider
from app.schemas.integration import (
    FeishuConfigPayload,
    FeishuConfigSource,
    FeishuIntegrationUpdate,
)
from app.services.feishu_integration import (
    EffectiveFeishuConfig,
    delete_feishu_integration,
    get_feishu_integration_status,
    require_configured_feishu_config,
    resolve_feishu_config,
    save_feishu_integration,
)
from app.services.feishu_integration import (
    test_feishu_connection as probe_feishu_connection,
)
from tests.feishu_helpers import add_feishu_config

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


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (None, ""),
        (
            "https://admin.example.com/auth/feishu/callback",
            "https://admin.example.com/auth/feishu/callback",
        ),
        (
            "  https://admin.example.com/callback  ",
            "https://admin.example.com/callback",
        ),
    ],
)
def test_update_normalizes_allowed_redirect_uri(
    redirect_uri: str | None,
    expected: str,
) -> None:
    payload = FeishuIntegrationUpdate(
        app_id="  cli-test  ",
        app_secret="  secret-value  ",
        redirect_uri=redirect_uri,
    )
    assert payload.app_id == "cli-test"
    assert payload.app_secret == "secret-value"
    assert payload.redirect_uri == expected


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://admin.example.com/callback",
        "/relative",
        "https://example.com/a#fragment",
        "https://example.com/callback#",
        "https://" + "a" * 2040,
    ],
)
def test_update_rejects_invalid_redirect_uri(redirect_uri: str) -> None:
    with pytest.raises(ValidationError):
        FeishuIntegrationUpdate(
            app_id="cli-test",
            app_secret=None,
            redirect_uri=redirect_uri,
        )


def test_update_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FeishuIntegrationUpdate(
            app_id="cli-test",
            app_secret=None,
            unexpected="value",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("app_secret", [None, "", "   "])
def test_update_normalizes_blank_app_secret(app_secret: str | None) -> None:
    payload = FeishuIntegrationUpdate(
        app_id="cli-test",
        app_secret=app_secret,
    )

    assert payload.app_secret is None


@pytest.mark.parametrize("app_secret", ["", "   "])
def test_payload_rejects_blank_app_secret(app_secret: str) -> None:
    with pytest.raises(ValidationError):
        FeishuConfigPayload(
            version=1,
            app_id="cli-test",
            app_secret=app_secret,
            redirect_uri="",
        )


def test_secret_is_excluded_from_model_reprs() -> None:
    payload = FeishuConfigPayload(
        version=1,
        app_id="cli-test",
        app_secret="payload-secret",
        redirect_uri="",
    )
    update = FeishuIntegrationUpdate(
        app_id="cli-test",
        app_secret="update-secret",
    )

    assert "payload-secret" not in repr(payload)
    assert "update-secret" not in repr(update)


def test_validation_error_does_not_expose_app_secret() -> None:
    secret = "secret-" + "x" * 512

    with pytest.raises(ValidationError) as error:
        FeishuIntegrationUpdate(
            app_id="cli-test",
            app_secret=secret,
        )

    assert secret not in str(error.value)


async def test_resolve_prefers_valid_database_config(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    await add_feishu_config(session, app_id="cli-db", app_secret="db-secret")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.DATABASE
    assert config.app_id == "cli-db"
    assert config.app_secret == "db-secret"


async def test_resolve_falls_back_to_environment(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", " cli-env ")
    monkeypatch.setattr(settings, "feishu_app_secret", " env-secret ")
    monkeypatch.setattr(settings, "feishu_redirect_uri", "")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.ENVIRONMENT
    assert config.app_id == "cli-env"
    assert config.app_secret == "env-secret"
    assert config.redirect_uri == ""
    assert config.updated_at is None


async def test_resolve_accepts_valid_https_environment_redirect_uri(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    monkeypatch.setattr(
        settings,
        "feishu_redirect_uri",
        "  https://admin.example.com/auth/feishu/callback  ",
    )

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.ENVIRONMENT
    assert config.redirect_uri == "https://admin.example.com/auth/feishu/callback"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://admin.example.com/callback",
        "/relative",
        "https://example.com/callback#",
    ],
)
async def test_resolve_rejects_invalid_environment_redirect_uri(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    redirect_uri: str,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    monkeypatch.setattr(settings, "feishu_redirect_uri", redirect_uri)

    with pytest.raises(BusinessError) as error:
        await resolve_feishu_config(session)

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert redirect_uri not in error.value.message


async def test_resolve_returns_none_source_when_environment_is_incomplete(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.NONE
    assert config.app_id == ""
    assert config.app_secret == ""


async def test_damaged_database_config_never_falls_back_to_environment(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await resolve_feishu_config(session)

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert error.value.http_status == 503


@pytest.mark.parametrize(
    "plaintext",
    [
        "not-json",
        '{"version":2,"app_id":"cli-x","app_secret":"x","redirect_uri":""}',
        '{"version":1,"app_id":"","app_secret":"x","redirect_uri":""}',
        '{"version":1,"app_id":"cli-x","app_secret":"x","redirect_uri":"http://insecure"}',
        '{"version":1,"app_id":"cli-x","redirect_uri":""}',
    ],
)
async def test_invalid_decrypted_payload_is_unreadable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    plaintext: str,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext=encrypt_secret(plaintext),
            updated_by=1,
        )
    )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await resolve_feishu_config(session)

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert error.value.http_status == 503


async def test_status_never_contains_app_secret(session: AsyncSession) -> None:
    await add_feishu_config(session, app_secret="must-not-leak")

    status = await get_feishu_integration_status(session)

    assert status.source is FeishuConfigSource.DATABASE
    assert "must-not-leak" not in status.model_dump_json()


def test_require_configured_rejects_none_source() -> None:
    config = EffectiveFeishuConfig(
        source=FeishuConfigSource.NONE,
        app_id="",
        app_secret="",
        redirect_uri="",
    )

    with pytest.raises(BusinessError) as error:
        require_configured_feishu_config(config)

    assert error.value.code == ErrorCode.FEISHU_NOT_CONFIGURED
    assert error.value.http_status == 409


def test_require_configured_returns_effective_config() -> None:
    config = EffectiveFeishuConfig(
        source=FeishuConfigSource.ENVIRONMENT,
        app_id="cli-test",
        app_secret="secret-value",
        redirect_uri="",
    )

    assert require_configured_feishu_config(config) is config


async def test_first_save_without_secret_fails_before_upstream(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    verify = AsyncMock()
    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert verify.await_count == 0
    assert await get_by_provider(session, "feishu") is None


async def test_update_reuses_database_secret(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session, app_secret="old-secret")
    seen: list[tuple[str, str]] = []

    async def verify(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    status = await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(
            app_id="cli-updated",
            app_secret="   ",
            redirect_uri="https://admin.example.com/new-callback",
        ),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    assert row is not None
    stored = FeishuConfigPayload.model_validate_json(
        decrypt_secret(row.secret_ciphertext)
    )
    assert seen == [("cli-updated", "old-secret")]
    assert stored.app_secret == "old-secret"
    assert status.app_id == "cli-updated"
    assert "old-secret" not in status.model_dump_json()


async def test_removed_app_id_with_reused_secret_does_not_replace_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await add_feishu_config(session, app_id="cli-old", app_secret="old-secret")
    old_ciphertext = row.secret_ciphertext
    seen: list[tuple[str, str]] = []

    async def reject(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))
        raise BusinessError(
            ErrorCode.FEISHU_CREDENTIALS_REJECTED,
            "Feishu credentials rejected",
            http_status=422,
        )

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", reject)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    await session.refresh(row)
    assert seen == [("cli-new", "old-secret")]
    assert error.value.code == ErrorCode.FEISHU_CREDENTIALS_REJECTED
    assert row.secret_ciphertext == old_ciphertext


async def test_new_secret_can_repair_damaged_database_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    status = await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-repaired", app_secret="new-secret"),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    assert row is not None
    stored = FeishuConfigPayload.model_validate_json(
        decrypt_secret(row.secret_ciphertext)
    )
    assert status.source is FeishuConfigSource.DATABASE
    assert stored.app_id == "cli-repaired"
    assert stored.app_secret == "new-secret"


async def test_update_without_secret_cannot_repair_damaged_database_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    verify = AsyncMock()
    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert verify.await_count == 0


async def test_update_without_database_row_can_reuse_environment_secret(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-db", app_secret=None),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    assert row is not None
    stored = FeishuConfigPayload.model_validate_json(
        decrypt_secret(row.secret_ciphertext)
    )
    assert stored.app_secret == "env-secret"


async def test_saved_payload_is_encrypted_and_not_plaintext(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(
            app_id="cli-secure",
            app_secret="very-secret-value",
            redirect_uri="https://admin.example.com/callback",
        ),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    assert row is not None
    assert "very-secret-value" not in row.secret_ciphertext
    assert "cli-secure" not in row.secret_ciphertext


async def test_test_connection_uses_effective_database_config(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session, app_id="cli-db", app_secret="db-secret")
    seen: list[tuple[str, str]] = []

    async def verify(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    await probe_feishu_connection(session)

    assert seen == [("cli-db", "db-secret")]


async def test_delete_reveals_environment_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session)
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")

    status = await delete_feishu_integration(session, context=context)

    assert await get_by_provider(session, "feishu") is None
    assert status.configured is True
    assert status.source is FeishuConfigSource.ENVIRONMENT
    assert status.app_id == "cli-env"


async def test_repeated_delete_is_idempotent(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")

    first = await delete_feishu_integration(session, context=context)
    second = await delete_feishu_integration(session, context=context)

    assert first.source is FeishuConfigSource.NONE
    assert second.source is FeishuConfigSource.NONE
    assert second.configured is False


async def test_test_connection_rejects_unconfigured_and_damaged_sources(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    with pytest.raises(BusinessError) as missing:
        await probe_feishu_connection(session)
    assert missing.value.code == ErrorCode.FEISHU_NOT_CONFIGURED

    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    with pytest.raises(BusinessError) as damaged:
        await probe_feishu_connection(session)
    assert damaged.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
