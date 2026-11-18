"""Fitness data sources and which of them may earn credits.

This is the one place that knows sources by name. Everything downstream — validation,
de-duplication, the reward engine — works on normalised records and asks this module
only two questions: "which sources can earn?" and "which source is authoritative for
this day?".

Adding a source (Strava, Health Connect, ...) means: a new ``FitnessSource`` member, a
one-line migration extending the CHECK constraints, a place in ``SOURCE_PRIORITY`` and an
adapter that produces ``SyncBatch`` objects. Nothing else changes.
"""

from collections.abc import Iterable

from app.core.config import Settings
from app.models.enums import FitnessSource

# Highest priority first. A day is paid from exactly one source — the first in this
# order that has data for it — so two sources reporting the same day are never summed.
SOURCE_PRIORITY: tuple[FitnessSource, ...] = (
    FitnessSource.APPLE_HEALTHKIT,
    FitnessSource.TEST,
)

# Sources whose records count as verified activity when they pass validation.
# ``manual`` is stored for history but is never rewarded automatically.
_ALWAYS_REWARDABLE = frozenset({FitnessSource.APPLE_HEALTHKIT})


def rewardable_sources(settings: Settings) -> tuple[FitnessSource, ...]:
    """Sources allowed to earn credits under the current configuration, by priority.

    Test data earns only where it has been explicitly enabled, which production
    configuration forbids.
    """
    allowed = set(_ALWAYS_REWARDABLE)
    if settings.allow_test_fitness_data:
        allowed.add(FitnessSource.TEST)
    return tuple(source for source in SOURCE_PRIORITY if source in allowed)


def canonical_source(present: Iterable[str], settings: Settings) -> FitnessSource | None:
    """The authoritative source for a day, given the sources that have data for it."""
    available = set(present)
    for source in rewardable_sources(settings):
        if source.value in available:
            return source
    return None
