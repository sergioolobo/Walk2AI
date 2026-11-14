"""Local-day arithmetic.

Every timestamp is stored in UTC. A user's "day" — the unit the daily reward cap and
daily spending limits apply to — is a calendar day in the user's IANA timezone
(Europe/Lisbon by default). These helpers convert between the two, and are the only
place that does so. Daylight saving is handled by ``zoneinfo``: a local day is 23 or
25 hours long on transition days, and the boundaries below reflect that.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, available_timezones


def is_valid_timezone(name: str) -> bool:
    return name in available_timezones()


def _require_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("a timezone-aware datetime is required")


def local_date(instant: datetime, tz: str) -> date:
    """The calendar date in ``tz`` at the given instant."""
    _require_aware(instant)
    return instant.astimezone(ZoneInfo(tz)).date()


def day_bounds_utc(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC instants [start, end) of local calendar day ``day`` in ``tz``."""
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def month_bounds_utc(year: int, month: int, tz: str) -> tuple[datetime, datetime]:
    """UTC instants [start, end) of the local calendar month in ``tz``."""
    zone = ZoneInfo(tz)
    first = date(year, month, 1)
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    start = datetime.combine(first, time.min, tzinfo=zone)
    end = datetime.combine(next_first, time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
