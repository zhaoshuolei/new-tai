from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.integration_config import IntegrationConfig
from app.schemas.integration import FeishuConfigPayload


async def add_feishu_config(
    session: AsyncSession,
    *,
    app_id: str = "cli-test",
    app_secret: str = "secret-value",
    redirect_uri: str = "https://admin.example.com/auth/feishu/callback",
    updated_by: int = 7,
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
        updated_by=updated_by,
    )
    session.add(row)
    await session.flush()
    return row
