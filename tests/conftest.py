import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

os.environ.setdefault(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("JWT_SECRET", "test-secret")

from app.core.config import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        get: bool = False,
    ) -> str | bool | None:
        existing = self.values.get(name)
        if nx and existing is not None:
            return None if get else False
        if xx and existing is None:
            return None if get else False
        self.values[name] = value
        if ex is not None:
            self.expirations[name] = ex
        return existing if get else True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
                self.expirations.pop(name, None)
        return deleted

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        if "redis.call('INCR'" not in script:
            raise NotImplementedError

        keys = args[:numkeys]
        values = args[numkeys:]
        ttl = int(values[0])
        threshold = int(values[1])
        exceeded = 0
        for redis_key in keys:
            name = str(redis_key)
            count = int(self.values.get(name, "0")) + 1
            self.values[name] = str(count)
            if count == 1:
                self.expirations[name] = ttl
            if count > threshold:
                exceeded = 1
        return exceeded


@pytest_asyncio.fixture(autouse=True)
async def _dispose_global_engine_after_test() -> AsyncIterator[None]:
    import app.db.session as db_session

    yield
    await db_session.engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url=(
            "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test"
        ),
        redis_url="redis://localhost:6379/15",
        jwt_secret="test-secret",
    )
