import importlib
import zoneinfo
from datetime import UTC, date, datetime, timedelta, tzinfo

import pytest

import app.core.datetime_utils as datetime_utils
from app.core.datetime_utils import (
    BUSINESS_TIMEZONE,
    business_day_bounds_utc,
    business_today,
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        instant = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
        if tz is None:
            return instant.replace(tzinfo=None)
        return instant.astimezone(tz)


def test_business_today_uses_shanghai_calendar_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(datetime_utils, "datetime", FixedDateTime)

    assert business_today() == date(2026, 10, 18)


def test_business_timezone_falls_back_without_tzdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_zoneinfo(key: str) -> tzinfo:
        raise zoneinfo.ZoneInfoNotFoundError(key)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(zoneinfo, "ZoneInfo", missing_zoneinfo)
            reloaded = importlib.reload(datetime_utils)

            resolved = reloaded.datetime(2026, 1, 1, tzinfo=reloaded.BUSINESS_TIMEZONE)
            assert resolved.utcoffset() == timedelta(hours=8)
    finally:
        importlib.reload(datetime_utils)

    assert BUSINESS_TIMEZONE is not None


def test_business_day_bounds_use_shanghai_midnight() -> None:
    # 2026-10-17 20:00 UTC = 2026-10-18 04:00 上海，因此今日边界是
    # 上海 2026-10-18 00:00 = UTC 2026-10-17 16:00。
    reference = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
    start, end = business_day_bounds_utc(reference)
    assert start == datetime(2026, 10, 17, 16, 0)
    assert end == datetime(2026, 10, 18, 16, 0)
    assert start.tzinfo is None and end.tzinfo is None


def test_business_day_bounds_accept_naive_reference_as_utc() -> None:
    reference = datetime(2026, 10, 17, 20, 0)
    start, end = business_day_bounds_utc(reference)
    assert (start, end) == (
        datetime(2026, 10, 17, 16, 0),
        datetime(2026, 10, 18, 16, 0),
    )
