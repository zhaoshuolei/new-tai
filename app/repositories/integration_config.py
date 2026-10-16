"""第三方集成配置数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration_config import IntegrationConfig


async def get_by_provider(
    session: AsyncSession,
    provider: str,
) -> IntegrationConfig | None:
    statement = select(IntegrationConfig).where(IntegrationConfig.provider == provider)
    return await session.scalar(statement)


async def upsert(
    session: AsyncSession,
    provider: str,
    ciphertext: str,
    updated_by: int,
) -> IntegrationConfig:
    # 仅 flush 不 commit，事务边界由服务层控制。
    config = await get_by_provider(session, provider)
    if config is None:
        config = IntegrationConfig(provider=provider)
        session.add(config)
    config.secret_ciphertext = ciphertext
    config.updated_by = updated_by
    await session.flush()
    return config
