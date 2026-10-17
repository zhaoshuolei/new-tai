from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_media_root(value: str) -> Path:
    stripped = value.strip()
    if not stripped:
        raise ValueError("media_root must not be empty")

    media_root = Path(stripped).expanduser().resolve()
    working_directory = Path.cwd().resolve()
    if media_root == working_directory or media_root in working_directory.parents:
        raise ValueError(
            "media_root must not be the current working directory or an ancestor"
        )
    return media_root


def normalize_media_url_path(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("media_url_path must not be empty")

    if any(character in stripped for character in ("?", "#", "\\")):
        raise ValueError(
            "media_url_path must be a URL path without a query, fragment, or backslash"
        )

    segments = [segment for segment in stripped.split("/") if segment]
    if not segments:
        raise ValueError("media_url_path must not be the root path")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("media_url_path must not contain dot segments")
    return "/" + "/".join(segments)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "tianxiadmin"
    env: Literal["local", "test", "prod"] = "local"
    database_url: str
    redis_url: str
    jwt_secret: str
    config_encryption_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    cors_origins: list[str] = []
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_redirect_uri: str = ""
    initial_superadmin_username: str = "superadmin"
    initial_superadmin_password: str = ""
    monitor_enabled: bool = True
    monitor_sample_interval_seconds: int = Field(default=60, ge=1)
    monitor_metric_retention_days: int = 30
    exception_log_retention_days: int = 90
    login_log_retention_days: int = 180
    audit_log_retention_days: int = 365
    media_root: str = Field(default="media", validate_default=True)
    media_url_path: str = Field(default="/media", validate_default=True)
    system_asset_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)

    @field_validator("media_root")
    @classmethod
    def _validate_media_root(cls, value: str) -> str:
        return str(resolve_media_root(value))

    @field_validator("media_url_path")
    @classmethod
    def _validate_media_url_path(cls, value: str) -> str:
        return normalize_media_url_path(value)


settings = Settings()
