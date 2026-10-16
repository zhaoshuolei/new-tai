import os

import pytest

os.environ.setdefault("DATABASE_URL", "mysql+asyncmy://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("JWT_SECRET", "test-secret")

from app.core.config import Settings


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="mysql+asyncmy://test:test@localhost/test",
        redis_url="redis://localhost:6379/15",
        jwt_secret="test-secret",
    )
