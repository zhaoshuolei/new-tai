"""第三方集成配置数据访问。"""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
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
    """原子插入或更新一行，避免保存与删除并发时产生陈旧 ORM 写入。"""
    statement = mysql_insert(IntegrationConfig).values(
        provider=provider,
        secret_ciphertext=ciphertext,
        updated_by=updated_by,
    )
    await session.execute(
        statement.on_duplicate_key_update(
            secret_ciphertext=statement.inserted.secret_ciphertext,
            updated_by=statement.inserted.updated_by,
            updated_at=func.utc_timestamp(6),
        )
    )

    config = await session.scalar(
        select(IntegrationConfig)
        .where(IntegrationConfig.provider == provider)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if config is None:
        raise RuntimeError("integration configuration upsert did not persist")

    # 仅 flush 不 commit，事务边界由服务层控制。
    await session.flush()
    return config


async def delete_by_provider(
    session: AsyncSession,
    provider: str,
) -> bool:
    result = await session.execute(
        delete(IntegrationConfig).where(IntegrationConfig.provider == provider)
    )
    return bool(getattr(result, "rowcount", 0) or 0)
