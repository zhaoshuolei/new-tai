import os

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "mysql+asyncmy://test:test@localhost/test")
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


@pytest_asyncio.fixture
async def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="mysql+asyncmy://test:test@localhost/test",
        redis_url="redis://localhost:6379/15",
        jwt_secret="test-secret",
    )
