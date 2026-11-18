"""Local-day arithmetic, with Europe/Lisbon (WET in winter, WEST in summer) as the case
that matters: the daily reward cap resets at Portuguese midnight, whatever UTC says."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.timeutil import day_bounds_utc, is_valid_timezone, local_date, month_bounds_utc

LISBON = "Europe/Lisbon"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_winter_day_in_lisbon_matches_utc() -> None:
    start, end = day_bounds_utc(date(2026, 1, 15), LISBON)
    assert (start, end) == (utc(2026, 1, 15, 0, 0), utc(2026, 1, 16, 0, 0))


def test_summer_day_in_lisbon_starts_at_23h_utc_the_evening_before() -> None:
    start, end = day_bounds_utc(date(2026, 7, 15), LISBON)
    assert (start, end) == (utc(2026, 7, 14, 23, 0), utc(2026, 7, 15, 23, 0))


def test_spring_forward_day_is_23_hours_long() -> None:
    """29 March 2026: clocks jump from 01:00 to 02:00 in Lisbon."""
    start, end = day_bounds_utc(date(2026, 3, 29), LISBON)
    assert start == utc(2026, 3, 29, 0, 0)  # midnight WET
    assert end == utc(2026, 3, 29, 23, 0)  # next midnight is already WEST
    assert end - start == timedelta(hours=23)


def test_autumn_back_day_is_25_hours_long() -> None:
    """25 October 2026: clocks fall back from 02:00 to 01:00 in Lisbon."""
    start, end = day_bounds_utc(date(2026, 10, 25), LISBON)
    assert start == utc(2026, 10, 24, 23, 0)  # midnight WEST
    assert end == utc(2026, 10, 26, 0, 0)  # next midnight is WET
    assert end - start == timedelta(hours=25)


def test_consecutive_days_tile_time_without_gaps_or_overlaps() -> None:
    day = date(2026, 1, 1)
    _, previous_end = day_bounds_utc(day - timedelta(days=1), LISBON)
    for _ in range(366):
        start, end = day_bounds_utc(day, LISBON)
        assert start == previous_end
        assert end > start
        previous_end = end
        day += timedelta(days=1)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # Winter: Lisbon is on UTC.
        (utc(2026, 1, 15, 23, 59), date(2026, 1, 15)),
        (utc(2026, 1, 16, 0, 0), date(2026, 1, 16)),
        # Summer: 23:30 UTC is already 00:30 the next day in Lisbon.
        (utc(2026, 7, 14, 22, 59), date(2026, 7, 14)),
        (utc(2026, 7, 14, 23, 0), date(2026, 7, 15)),
        (utc(2026, 7, 14, 23, 30), date(2026, 7, 15)),
        # Around the transitions.
        (utc(2026, 3, 29, 0, 59), date(2026, 3, 29)),
        (utc(2026, 3, 29, 23, 0), date(2026, 3, 30)),
        (utc(2026, 10, 24, 22, 59), date(2026, 10, 24)),
        (utc(2026, 10, 24, 23, 0), date(2026, 10, 25)),
        (utc(2026, 10, 25, 23, 59), date(2026, 10, 25)),
    ],
)
def test_local_date_in_lisbon(instant: datetime, expected: date) -> None:
    assert local_date(instant, LISBON) == expected


def test_local_date_agrees_with_day_bounds_for_every_hour_of_a_year() -> None:
    instant = utc(2026, 1, 1, 0, 0)
    for _ in range(365 * 24):
        start, end = day_bounds_utc(local_date(instant, LISBON), LISBON)
        assert start <= instant < end
        instant += timedelta(hours=1)


def test_local_date_accepts_any_aware_datetime_but_not_a_naive_one() -> None:
    from zoneinfo import ZoneInfo

    tokyo_morning = datetime(2026, 7, 15, 7, 30, tzinfo=ZoneInfo("Asia/Tokyo"))  # 22:30 UTC, 14th
    assert local_date(tokyo_morning, LISBON) == date(2026, 7, 14)
    with pytest.raises(ValueError, match="timezone-aware"):
        local_date(datetime(2026, 7, 15, 7, 30), LISBON)  # noqa: DTZ001 - the point of the test


def test_month_bounds_follow_local_midnight() -> None:
    assert month_bounds_utc(2026, 1, LISBON) == (utc(2026, 1, 1), utc(2026, 2, 1))
    # July starts and ends in summer time.
    assert month_bounds_utc(2026, 7, LISBON) == (utc(2026, 6, 30, 23), utc(2026, 7, 31, 23))
    # March starts in winter time and ends in summer time; October the reverse.
    assert month_bounds_utc(2026, 3, LISBON) == (utc(2026, 3, 1), utc(2026, 3, 31, 23))
    assert month_bounds_utc(2026, 10, LISBON) == (utc(2026, 9, 30, 23), utc(2026, 11, 1))
    # December rolls over into the next year.
    assert month_bounds_utc(2026, 12, LISBON) == (utc(2026, 12, 1), utc(2027, 1, 1))


def test_other_timezones_use_their_own_midnight() -> None:
    start, end = day_bounds_utc(date(2026, 7, 15), "America/New_York")
    assert (start, end) == (utc(2026, 7, 15, 4, 0), utc(2026, 7, 16, 4, 0))
    assert local_date(utc(2026, 7, 15, 3, 59), "America/New_York") == date(2026, 7, 14)


def test_timezone_validation() -> None:
    assert is_valid_timezone("Europe/Lisbon")
    assert is_valid_timezone("UTC")
    assert not is_valid_timezone("Europe/Lisboa")
    assert not is_valid_timezone("")
    assert not is_valid_timezone("GMT+1; DROP TABLE users")
