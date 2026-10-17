"""飞书登录配置解析服务。"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.feishu import FeishuClient
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import BusinessError, ErrorCode
from app.repositories.integration_config import (
    delete_by_provider,
    get_by_provider,
    upsert,
)
from app.schemas.integration import (
    FeishuConfigPayload,
    FeishuConfigSource,
    FeishuIntegrationStatus,
    FeishuIntegrationUpdate,
    validate_redirect_uri,
)
from app.services.audit import audited

_PROVIDER = "feishu"
_UNREADABLE_MESSAGE = "Feishu configuration cannot be read"


@dataclass(frozen=True, slots=True)
class EffectiveFeishuConfig:
    source: FeishuConfigSource
    app_id: str
    app_secret: str = field(repr=False)
    redirect_uri: str
    updated_at: datetime | None = None
    updated_by: int | None = None


def _unreadable_config_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FEISHU_CONFIG_UNREADABLE,
        _UNREADABLE_MESSAGE,
        http_status=503,
    )


async def resolve_feishu_config(session: AsyncSession) -> EffectiveFeishuConfig:
    """解析数据库配置；数据库损坏时终止，绝不回退到环境配置。"""
    row = await get_by_provider(session, _PROVIDER)
    if row is not None:
        plaintext = decrypt_secret(
            row.secret_ciphertext,
            error_code=ErrorCode.FEISHU_CONFIG_UNREADABLE,
            error_message=_UNREADABLE_MESSAGE,
        )
        try:
            payload = FeishuConfigPayload.model_validate_json(plaintext)
        except ValidationError:
            raise _unreadable_config_error() from None

        return EffectiveFeishuConfig(
            source=FeishuConfigSource.DATABASE,
            app_id=payload.app_id,
            app_secret=payload.app_secret,
            redirect_uri=payload.redirect_uri,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )

    app_id = settings.feishu_app_id.strip()
    app_secret = settings.feishu_app_secret.strip()
    if not app_id or not app_secret:
        return EffectiveFeishuConfig(
            source=FeishuConfigSource.NONE,
            app_id="",
            app_secret="",
            redirect_uri="",
        )

    try:
        redirect_uri = validate_redirect_uri(settings.feishu_redirect_uri)
    except ValueError:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Feishu redirect URI is invalid",
            http_status=422,
        ) from None

    return EffectiveFeishuConfig(
        source=FeishuConfigSource.ENVIRONMENT,
        app_id=app_id,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
    )


def require_configured_feishu_config(
    config: EffectiveFeishuConfig,
) -> EffectiveFeishuConfig:
    if config.source is FeishuConfigSource.NONE:
        raise BusinessError(
            ErrorCode.FEISHU_NOT_CONFIGURED,
            "Feishu is not configured",
            http_status=409,
        )
    return config


async def get_feishu_integration_status(
    session: AsyncSession,
) -> FeishuIntegrationStatus:
    config = await resolve_feishu_config(session)
    configured = config.source is not FeishuConfigSource.NONE
    return FeishuIntegrationStatus(
        configured=configured,
        source=config.source,
        app_id=config.app_id or None,
        redirect_uri=config.redirect_uri or None,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


def build_feishu_client(config: EffectiveFeishuConfig) -> FeishuClient:
    return FeishuClient(
        app_id=config.app_id,
        app_secret=config.app_secret,
        redirect_uri=config.redirect_uri or None,
    )


@audited(action="integration_feishu:update", resource_type="integration")
async def save_feishu_integration(
    session: AsyncSession,
    payload: FeishuIntegrationUpdate,
    *,
    context: AuthContext,
    client: FeishuClient | None = None,
) -> FeishuIntegrationStatus:
    requested_secret = payload.app_secret
    if requested_secret is None:
        try:
            current_config = require_configured_feishu_config(
                await resolve_feishu_config(session)
            )
        except BusinessError as error:
            if error.code != ErrorCode.FEISHU_NOT_CONFIGURED:
                raise
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                "Feishu app secret is required",
                http_status=422,
            ) from None
        final_secret = current_config.app_secret
    else:
        final_secret = requested_secret

    candidate = client or FeishuClient(
        app_id=payload.app_id,
        app_secret=final_secret,
    )
    await candidate.verify_app_credentials()

    stored_payload = FeishuConfigPayload(
        version=1,
        app_id=payload.app_id,
        app_secret=final_secret,
        redirect_uri=payload.redirect_uri or "",
    )
    row = await upsert(
        session,
        _PROVIDER,
        encrypt_secret(stored_payload.model_dump_json()),
        updated_by=context.user_id,
    )
    await session.commit()

    persisted_payload = FeishuConfigPayload.model_validate_json(
        decrypt_secret(row.secret_ciphertext)
    )
    return FeishuIntegrationStatus(
        configured=True,
        source=FeishuConfigSource.DATABASE,
        app_id=persisted_payload.app_id,
        redirect_uri=persisted_payload.redirect_uri or None,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def test_feishu_connection(
    session: AsyncSession,
    *,
    client: FeishuClient | None = None,
) -> None:
    config = require_configured_feishu_config(await resolve_feishu_config(session))
    probe = client or build_feishu_client(config)
    await probe.verify_app_credentials()


@audited(action="integration_feishu:update", resource_type="integration")
async def delete_feishu_integration(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> FeishuIntegrationStatus:
    await delete_by_provider(session, _PROVIDER)
    await session.commit()
    return await get_feishu_integration_status(session)


__all__: tuple[str, ...] = (
    "EffectiveFeishuConfig",
    "build_feishu_client",
    "delete_feishu_integration",
    "get_feishu_integration_status",
    "require_configured_feishu_config",
    "resolve_feishu_config",
    "save_feishu_integration",
    "test_feishu_connection",
)
