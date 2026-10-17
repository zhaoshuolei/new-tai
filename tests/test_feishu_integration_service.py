"""Feishu integration configuration schema and service tests."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.errors import BusinessError, ErrorCode
from app.models.integration_config import IntegrationConfig
from app.schemas.integration import (
    FeishuConfigPayload,
    FeishuConfigSource,
    FeishuIntegrationUpdate,
)
from app.services.feishu_integration import (
    EffectiveFeishuConfig,
    get_feishu_integration_status,
    require_configured_feishu_config,
    resolve_feishu_config,
)

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
            unexpected="value",
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
    assert config.updated_at is None


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


async def add_feishu_config(
    session: AsyncSession,
    *,
    app_id: str = "cli-test",
    app_secret: str = "secret-value",
    redirect_uri: str = "https://admin.example.com/auth/feishu/callback",
) -> IntegrationConfig:
    payload = FeishuConfigPayload(
        version=1,
        app_id=app_id,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
    )
    row = IntegrationConfig(
        provider="feishu",
        secret_ciphertext=encrypt_secret(payload.model_dump_json()),
        updated_by=7,
    )
    session.add(row)
    await session.flush()
    return row
