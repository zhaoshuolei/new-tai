"""Datetime normalization helpers."""

from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
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


def business_day_bounds_utc(
    reference: datetime | None = None,
) -> tuple[datetime, datetime]:
    """返回业务时区「今日/明日 00:00」的朴素 UTC 边界。"""
    reference_utc = to_utc_naive(datetime.now(UTC) if reference is None else reference)
    assert reference_utc is not None

    business_date = (
        reference_utc.replace(tzinfo=UTC).astimezone(BUSINESS_TIMEZONE).date()
    )
    start_local = datetime.combine(
        business_date,
        time.min,
        tzinfo=BUSINESS_TIMEZONE,
    )
    end_local = datetime.combine(
        business_date + timedelta(days=1),
        time.min,
        tzinfo=BUSINESS_TIMEZONE,
    )
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


__all__: tuple[str, ...] = (
    "BUSINESS_TIMEZONE",
    "business_day_bounds_utc",
    "business_today",
    "to_utc_naive",
)
