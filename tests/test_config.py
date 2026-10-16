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


def test_monitor_sample_interval_default_is_60(test_settings: Settings) -> None:
    assert test_settings.monitor_sample_interval_seconds == 60


@pytest.mark.parametrize("interval", [0, -1, -60])
def test_monitor_sample_interval_must_be_positive(interval: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="mysql+asyncmy://u:p@localhost:3306/db",
            redis_url="redis://localhost:6379/15",
            jwt_secret="secret",
            monitor_sample_interval_seconds=interval,
            _env_file=None,
        )
