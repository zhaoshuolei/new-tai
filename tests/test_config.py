from pathlib import Path

import pytest
from cryptography.fernet import Fernet
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


def test_monitor_sample_interval_default_is_60(test_settings: Settings) -> None:
    assert test_settings.monitor_sample_interval_seconds == 60


@pytest.mark.parametrize("interval", [0, -1, -60])
def test_monitor_sample_interval_must_be_positive(interval: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="mysql+asyncmy://u:p@localhost:3306/db",
            redis_url="redis://localhost:6379/15",
            jwt_secret="secret",
            config_encryption_key=Fernet.generate_key().decode(),
            monitor_sample_interval_seconds=interval,
            _env_file=None,
        )


def _settings(**overrides: object) -> Settings:
    values = {
        "database_url": "mysql+asyncmy://u:p@localhost:3306/db",
        "redis_url": "redis://localhost:6379/15",
        "jwt_secret": "secret",
        "config_encryption_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_attachment_root_defaults_below_working_directory(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(media_root=str(tmp_path / "media"))
    assert settings.follow_up_attachment_root.startswith(str(tmp_path))
    assert settings.follow_up_attachment_max_bytes == 10485760
    assert settings.follow_up_attachment_max_per_record == 5


@pytest.mark.parametrize("suffix", ["", "/nested", "/../media/nested"])
def test_attachment_root_must_not_overlap_media_root(
    tmp_path, monkeypatch, suffix
) -> None:
    monkeypatch.chdir(tmp_path)
    media_root = tmp_path / "media"
    with pytest.raises(ValidationError):
        _settings(
            media_root=str(media_root),
            follow_up_attachment_root=str(media_root) + suffix,
        )


def test_media_root_inside_attachment_root_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        _settings(
            media_root=str(tmp_path / "files" / "media"),
            follow_up_attachment_root=str(tmp_path / "files"),
        )


def test_data_import_defaults_below_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = _settings()

    assert Path(settings.data_import_root) == (tmp_path / "data-imports").resolve()
    assert settings.data_import_max_bytes == 52428800


@pytest.mark.parametrize("data_import_root", ["", "   ", ".", ".."])
def test_data_import_root_rejects_empty_current_or_ancestor(
    data_import_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    with pytest.raises(ValidationError):
        _settings(data_import_root=data_import_root)


@pytest.mark.parametrize(
    ("private_root", "relation"),
    [
        ("media", "same"),
        ("media", "descendant"),
        ("media", "ancestor"),
        ("follow_up", "same"),
        ("follow_up", "descendant"),
        ("follow_up", "ancestor"),
    ],
)
def test_data_import_root_must_not_overlap_private_roots(
    private_root: str,
    relation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    private_base = tmp_path / "private"
    media_root = private_base / "media"
    follow_up_root = private_base / "attachments"
    if private_root == "media":
        media_root = media_root / "nested"
        overlapping_root = media_root
    else:
        follow_up_root = follow_up_root / "nested"
        overlapping_root = follow_up_root
    if relation == "descendant":
        overlapping_root = overlapping_root / "imports"
    elif relation == "ancestor":
        overlapping_root = overlapping_root.parent

    with pytest.raises(ValidationError):
        _settings(
            media_root=str(media_root),
            follow_up_attachment_root=str(follow_up_root),
            data_import_root=str(overlapping_root),
        )
