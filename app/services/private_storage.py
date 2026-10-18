"""Shared helpers for private file storage."""

import unicodedata

_MAX_ORIGINAL_NAME_LENGTH = 255


def sanitize_original_name(value: str | None) -> str | None:
    """Remove controls and path prefixes and cap a display-safe original name."""
    if value is None:
        return None

    without_controls = "".join(
        character for character in value if unicodedata.category(character) != "Cc"
    )
    last_segment = without_controls.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not last_segment:
        return None
    return last_segment[:_MAX_ORIGINAL_NAME_LENGTH]


__all__: tuple[str, ...] = ("sanitize_original_name",)
