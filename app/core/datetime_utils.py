"""Datetime normalization helpers."""

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    BUSINESS_TIMEZONE: tzinfo = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Normalize an aware datetime to naive UTC for DATETIME comparisons."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def business_today() -> date:
    """Return today's calendar date in the company's business timezone."""
    return datetime.now(BUSINESS_TIMEZONE).date()


__all__: tuple[str, ...] = ("BUSINESS_TIMEZONE", "business_today", "to_utc_naive")
