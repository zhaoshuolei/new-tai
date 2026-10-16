"""Datetime normalization helpers."""

from datetime import UTC, datetime


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Normalize an aware datetime to naive UTC for DATETIME comparisons."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


__all__: tuple[str, ...] = ("to_utc_naive",)
