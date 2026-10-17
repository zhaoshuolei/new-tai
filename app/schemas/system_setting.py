"""系统设置请求与响应模型。"""

import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)


def _validate_url_characters(value: str) -> None:
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ValueError("URL must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError("URL must not contain backslashes")


def _validate_http_url(value: str) -> str:
    _validate_url_characters(value)

    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("Invalid URL") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must use http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("URL must be absolute")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain credentials")

    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("URL contains an invalid port") from error
    return value


class SystemSettingUpdate(BaseModel):
    """完整替换全局系统设置。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    short_name: str | None = Field(default=None, max_length=50)
    site_url: str = Field(min_length=1, max_length=500)
    logo_url: str | None = Field(default=None, max_length=500)
    favicon_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    copyright: str | None = Field(default=None, max_length=200)
    icp_number: str | None = Field(default=None, max_length=100)

    @field_validator("*", mode="before")
    @classmethod
    def trim_text(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        if not trimmed and info.field_name not in {"name", "site_url"}:
            return None
        return trimmed

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("logo_url", "favicon_url")
    @classmethod
    def validate_asset_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("/"):
            _validate_url_characters(value)
            if value.startswith("//"):
                raise ValueError("Asset URL must be absolute or site-relative")
            return value
        return _validate_http_url(value)


class PublicSystemSettingRead(BaseModel):
    """公开系统设置。"""

    model_config = ConfigDict(from_attributes=True)

    name: str
    short_name: str | None
    site_url: str | None
    logo_url: str | None
    favicon_url: str | None
    description: str | None
    copyright: str | None
    icp_number: str | None


class SystemSettingRead(PublicSystemSettingRead):
    """后台系统设置，包含最后修改信息。"""

    updated_at: datetime | None
    updated_by: int | None


__all__: tuple[str, ...] = (
    "PublicSystemSettingRead",
    "SystemSettingRead",
    "SystemSettingUpdate",
)
