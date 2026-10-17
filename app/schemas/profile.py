"""Current-user profile request and response models."""

import re
import unicodedata
from datetime import date
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.datetime_utils import business_today
from app.models.user import UserStatus
from app.services.contact_validation import normalize_email, normalize_phone

_EXTENSION_PATTERN = re.compile(r"\d{1,16}")


def _validate_link_url_characters(value: str) -> None:
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ValueError("URL must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError("URL must not contain backslashes")


def _validate_http_url(value: str) -> str:
    _validate_link_url_characters(value)

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


def public_link_host(url: str) -> str | None:
    """Return the hostname used as the fallback for a blank link label."""
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


class PersonalLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    url: str = Field(max_length=500)

    @field_validator("label", mode="before")
    @classmethod
    def trim_label(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("url", mode="before")
    @classmethod
    def trim_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_http_url(value)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, max_length=100)
    signature: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=2000)
    work_phone: str | None = Field(default=None, max_length=32)
    extension: str | None = Field(default=None, max_length=16)
    work_email: str | None = Field(default=None, max_length=255)
    office_location: str | None = Field(default=None, max_length=100)
    seat: str | None = Field(default=None, max_length=50)
    personal_phone: str | None = Field(default=None, max_length=32)
    emergency_contact_name: str | None = Field(default=None, max_length=50)
    emergency_contact_phone: str | None = Field(default=None, max_length=32)
    birthday: date | None = None
    expertise_tags: list[str] | None = None
    personal_links: list[PersonalLinkInput] | None = None

    @field_validator(
        "nickname",
        "signature",
        "bio",
        "work_phone",
        "extension",
        "work_email",
        "office_location",
        "seat",
        "personal_phone",
        "emergency_contact_name",
        "emergency_contact_phone",
        mode="before",
    )
    @classmethod
    def trim_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        return trimmed or None

    @field_validator("work_phone", "personal_phone", "emergency_contact_phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_phone(value)
        except ValueError as error:
            raise ValueError("invalid phone number") from error

    @field_validator("extension")
    @classmethod
    def validate_extension(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _EXTENSION_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid extension")
        return value

    @field_validator("work_email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_email(value)
        except ValueError as error:
            raise ValueError("invalid email address") from error

    @field_validator("expertise_tags")
    @classmethod
    def validate_expertise_tags(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None

        result: list[str] = []
        seen: set[str] = set()
        for raw_tag in value:
            tag = raw_tag.strip()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            result.append(tag)

        if len(result) > 10:
            raise ValueError("expertise_tags must contain at most 10 items")
        if any(not 1 <= len(tag) <= 20 for tag in result):
            raise ValueError("expertise tags must be 1 to 20 characters")
        return result

    @field_validator("personal_links")
    @classmethod
    def validate_personal_links(
        cls,
        value: list[PersonalLinkInput] | None,
    ) -> list[PersonalLinkInput] | None:
        if value is None:
            return None

        result: list[PersonalLinkInput] = []
        for link in value:
            label = link.label.strip()
            if not label:
                host = public_link_host(link.url)
                if not host:
                    continue
                label = host

            if not 1 <= len(label) <= 30:
                raise ValueError("link labels must be 1 to 30 characters")
            result.append(PersonalLinkInput(label=label, url=link.url))

        if len(result) > 3:
            raise ValueError("personal_links must contain at most 3 items")
        return result

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, value: date | None) -> date | None:
        if value is not None and value > business_today():
            raise ValueError("birthday must not be in the future")
        return value


class PrivacyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hidden_fields: list[str]


class AvatarUploadRead(BaseModel):
    url: str | None


class PersonalLink(BaseModel):
    label: str
    url: str


class ProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    account_email: str | None
    department_id: int | None
    department_name: str | None
    user_status: UserStatus
    feishu_open_id: str | None
    nickname: str | None
    avatar: str | None
    signature: str | None
    bio: str | None
    work_phone: str | None
    extension: str | None
    work_email: str | None
    office_location: str | None
    seat: str | None
    expertise_tags: list[str]
    personal_links: list[PersonalLink]
    hidden_public_fields: list[str]
    personal_phone: str | None
    birthday: date | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None

    @field_serializer("birthday", when_used="json")
    def serialize_birthday(self, value: date | None) -> str | None:
        return value.isoformat() if value is not None else None


__all__: tuple[str, ...] = (
    "AvatarUploadRead",
    "PersonalLink",
    "PersonalLinkInput",
    "PrivacyUpdate",
    "ProfileRead",
    "ProfileUpdate",
    "public_link_host",
)
