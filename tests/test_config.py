import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_test_settings_fixture_injects_required_values(test_settings: Settings) -> None:
    assert test_settings.database_url.startswith("mysql+asyncmy://")
    assert test_settings.redis_url == "redis://localhost:6379/15"
    assert test_settings.jwt_secret == "test-secret"


def test_required_settings_fail_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.delenv("JWT_SECRET")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
