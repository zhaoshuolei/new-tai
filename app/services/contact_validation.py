"""联系人字段格式校验。"""

import re

_PHONE_PATTERN = re.compile(r"^\+?\d{7,20}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    normalized = normalized.replace(" ", "").replace("-", "")
    if _PHONE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("invalid phone number")
    return normalized


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    if len(normalized) > 255 or _EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("invalid email address")
    return normalized


__all__: tuple[str, ...] = ("normalize_email", "normalize_phone")
