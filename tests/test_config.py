import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import _validate_critical_settings


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


def test_config_encryption_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://u:p@h/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "s")
    monkeypatch.delenv("CONFIG_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_malformed_config_encryption_key_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_settings = Settings(
        database_url="mysql+asyncmy://u:p@h/db",
        redis_url="redis://localhost:6379/0",
        jwt_secret="s",
        config_encryption_key="not-a-fernet-key",
        _env_file=None,
    )
    monkeypatch.setattr("app.main.settings", malformed_settings)
    with pytest.raises(RuntimeError) as exc_info:
        _validate_critical_settings()
    assert str(exc_info.value) == "Invalid config_encryption_key"
