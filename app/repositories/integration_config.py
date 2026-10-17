"""第三方集成配置数据访问。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration_config import IntegrationConfig


async def get_by_provider(
    session: AsyncSession,
    provider: str,
) -> IntegrationConfig | None:
    statement = select(IntegrationConfig).where(IntegrationConfig.provider == provider)
    return await session.scalar(statement)


async def _get_by_provider_for_update(
    session: AsyncSession,
    provider: str,
) -> IntegrationConfig | None:
    """读取当前已提交的行。

    MySQL 默认 REPEATABLE READ：预检查的普通 SELECT 会固定快照，并发提交的
    行在回退查询里可能不可见；加锁读强制读取最新版本。
    """
    statement = (
        select(IntegrationConfig)
        .where(IntegrationConfig.provider == provider)
        .with_for_update()
    )
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
        # 并发下两个 PUT 可能都查不到行；把插入放进 SAVEPOINT，唯一键冲突时
        # 回退为更新已存在的那一行，避免裸 IntegrityError 冒泡成 500。
        try:
            async with session.begin_nested():
                config = IntegrationConfig(provider=provider)
                config.secret_ciphertext = ciphertext
                config.updated_by = updated_by
                session.add(config)
                await session.flush()
        except IntegrityError:
            config = await _get_by_provider_for_update(session, provider)
            if config is None:
                raise
    config.secret_ciphertext = ciphertext
    config.updated_by = updated_by
    await session.flush()
    # 刷新以加载数据库生成的 created_at/updated_at，避免惰性读取触发 MissingGreenlet。
    await session.refresh(config)
    return config
