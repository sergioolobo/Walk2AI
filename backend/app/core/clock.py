"""Injectable time source.

Business logic never calls ``datetime.now()`` directly: it asks a Clock. That keeps
token expiry, reservation expiry and day-boundary logic deterministic under test.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
