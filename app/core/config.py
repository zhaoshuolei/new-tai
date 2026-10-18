from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_follow_up_attachment_root_path(value: str) -> Path:
    stripped = value.strip()
    if not stripped:
        raise ValueError("follow_up_attachment_root must not be empty")

    attachment_root = Path(stripped).expanduser().resolve()
    working_directory = Path.cwd().resolve()
    if (
        attachment_root == working_directory
        or attachment_root in working_directory.parents
    ):
        raise ValueError(
            "follow_up_attachment_root must not be the current working directory "
            "or an ancestor"
        )
    return attachment_root


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


def resolve_follow_up_attachment_root(value: str, media_root: str) -> Path:
    attachment_root = _resolve_follow_up_attachment_root_path(value)
    resolved_media_root = Path(media_root).expanduser().resolve()
    if attachment_root.is_relative_to(
        resolved_media_root
    ) or resolved_media_root.is_relative_to(attachment_root):
        raise ValueError("follow_up_attachment_root must not overlap media_root")
    return attachment_root


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
    follow_up_attachment_root: str = Field(default="attachments", validate_default=True)
    follow_up_attachment_max_bytes: int = Field(default=10485760, ge=1)
    follow_up_attachment_max_per_record: int = Field(default=5, ge=1)

    @field_validator("media_root")
    @classmethod
    def _validate_media_root(cls, value: str) -> str:
        return str(resolve_media_root(value))

    @field_validator("media_url_path")
    @classmethod
    def _validate_media_url_path(cls, value: str) -> str:
        return normalize_media_url_path(value)

    @field_validator("follow_up_attachment_root")
    @classmethod
    def _validate_follow_up_attachment_root(cls, value: str) -> str:
        return str(_resolve_follow_up_attachment_root_path(value))

    @model_validator(mode="after")
    def _validate_follow_up_attachment_root_does_not_overlap_media(self) -> Self:
        resolve_follow_up_attachment_root(
            self.follow_up_attachment_root,
            self.media_root,
        )
        return self


settings = Settings()
