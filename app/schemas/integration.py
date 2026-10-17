"""飞书登录配置的请求、载荷与状态模型。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class FeishuConfigSource(StrEnum):
    DATABASE = "database"
    ENVIRONMENT = "environment"
    NONE = "none"


def _normalize_app_id(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _normalize_app_secret(value: object, *, required: bool) -> object:
    if value is None:
        if required:
            raise ValueError("app_secret must not be empty")
        return None
    if not isinstance(value, str):
        return value

    normalized = value.strip()
    if normalized:
        return normalized
    if required:
        raise ValueError("app_secret must not be empty")
    return None


def validate_redirect_uri(value: str | None) -> str:
    """校验并规范化飞书回调地址；空值表示未配置。"""
    if value is None:
        return ""

    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > 2048:
        raise ValueError("redirect_uri is too long")

    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("redirect_uri is invalid") from None

    if (
        parsed.scheme != "https"
        or hostname is None
        or len(hostname) > 253
        or "#" in normalized
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("redirect_uri must be an HTTPS URL without a fragment")
    return normalized


def _normalize_redirect_uri(value: object) -> object:
    if value is None or isinstance(value, str):
        return validate_redirect_uri(value)
    return value


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class FeishuConfigPayload(BaseModel):
    """版本化的飞书配置密文载荷；App Secret 不进入 repr 或校验异常。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: Literal[1]
    app_id: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=1, max_length=512, repr=False)
    redirect_uri: str = Field(default="", max_length=2048)

    @field_validator("app_id", mode="before")
    @classmethod
    def normalize_app_id(cls, value: object) -> object:
        return _normalize_app_id(value)

    @field_validator("app_secret", mode="before")
    @classmethod
    def normalize_app_secret(cls, value: object) -> object:
        return _normalize_app_secret(value, required=True)

    @field_validator("redirect_uri", mode="before")
    @classmethod
    def normalize_redirect_uri(cls, value: object) -> object:
        return _normalize_redirect_uri(value)


class FeishuIntegrationUpdate(BaseModel):
    """飞书配置更新请求；空白 App Secret 表示沿用已保存值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    app_id: str = Field(min_length=1, max_length=256)
    app_secret: str | None = Field(default=None, max_length=512, repr=False)
    redirect_uri: str | None = Field(default=None, max_length=2048)

    @field_validator("app_id", mode="before")
    @classmethod
    def normalize_app_id(cls, value: object) -> object:
        return _normalize_app_id(value)

    @field_validator("app_secret", mode="before")
    @classmethod
    def normalize_app_secret(cls, value: object) -> object:
        return _normalize_app_secret(value, required=False)

    @field_validator("redirect_uri", mode="before")
    @classmethod
    def normalize_redirect_uri(cls, value: object) -> object:
        return _normalize_redirect_uri(value)


class FeishuIntegrationStatus(BaseModel):
    """飞书配置状态，永不包含 App Secret。"""

    model_config = ConfigDict(from_attributes=True)

    configured: bool
    source: FeishuConfigSource
    app_id: str | None
    redirect_uri: str | None
    updated_at: datetime | None
    updated_by: int | None

    @field_serializer("updated_at", when_used="json")
    def serialize_updated_at(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


__all__: tuple[str, ...] = (
    "FeishuConfigPayload",
    "FeishuConfigSource",
    "FeishuIntegrationStatus",
    "FeishuIntegrationUpdate",
    "validate_redirect_uri",
)
