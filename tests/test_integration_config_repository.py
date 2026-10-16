import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.repositories.integration_config import get_by_provider, upsert

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


async def test_upsert_creates_then_updates_single_row(session: AsyncSession) -> None:
    first = await upsert(session, "tianyancha", "cipher-1", updated_by=1)
    second = await upsert(session, "tianyancha", "cipher-2", updated_by=2)
    assert first.id == second.id
    fetched = await get_by_provider(session, "tianyancha")
    assert fetched is not None
    assert fetched.secret_ciphertext == "cipher-2"
    assert fetched.updated_by == 2
