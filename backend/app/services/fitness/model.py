"""Normalised fitness records.

Every source adapter (Apple HealthKit today; Strava or others later) turns whatever its
API provides into these three shapes. Validation, storage, de-duplication and rewards
only ever see these — they do not know or care where a record came from.

Records carry *facts* only. There is no field for credits: a device or adapter cannot
tell the server what activity is worth.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.timeutil import local_date
from app.models.enums import FitnessSource

_WORKOUT_TYPE_PREFIX = "HKWorkoutActivityType"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")
VALID_WORKOUT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_ONE_MICROSECOND = timedelta(microseconds=1)


def normalise_workout_type(raw: str) -> str:
    """Canonical snake_case name for a workout type.

    Accepts HealthKit's own spellings as well as ours, so that
    ``traditionalStrengthTraining``, ``HKWorkoutActivityTypeTraditionalStrengthTraining``
    and ``Traditional Strength Training`` all become ``traditional_strength_training``.
    """
    name = raw.strip()
    if name.startswith(_WORKOUT_TYPE_PREFIX):
        name = name[len(_WORKOUT_TYPE_PREFIX) :]
    name = _CAMEL_BOUNDARY.sub("_", name).lower()
    return _NON_IDENTIFIER.sub("_", name).strip("_")


@dataclass(frozen=True)
class DayRecord:
    """One local calendar day's totals, as reported by the source."""

    activity_date: date
    steps: int
    distance_walking_running_m: Decimal | None = None
    distance_cycling_m: Decimal | None = None
    active_energy_kcal: Decimal | None = None


@dataclass(frozen=True)
class WorkoutRecord:
    external_id: str  # the source's own stable id (HealthKit: the workout UUID)
    workout_type: str  # normalised with normalise_workout_type
    start: datetime
    end: datetime
    active_seconds: int  # time actually exercising; excludes pauses
    distance_m: Decimal | None = None
    active_energy_kcal: Decimal | None = None
    origin_bundle_id: str | None = None  # the app that recorded it (e.g. Mi Fitness)
    origin_device: str | None = None
    was_user_entered: bool = False

    def local_dates(self, timezone: str) -> tuple[date, ...]:
        """Every local calendar day this workout touches."""
        first = local_date(self.start, timezone)
        last = local_date(self.end - _ONE_MICROSECOND, timezone)
        days = (last - first).days
        return tuple(first + timedelta(days=offset) for offset in range(max(days, 0) + 1))


@dataclass(frozen=True)
class SyncBatch:
    """Everything one synchronisation delivers."""

    batch_id: uuid.UUID  # chosen by the client; makes a retried sync idempotent
    source: FitnessSource
    days: tuple[DayRecord, ...] = ()
    workouts: tuple[WorkoutRecord, ...] = ()
    deleted_workout_ids: tuple[str, ...] = ()
    device_timezone: str | None = None

    def fingerprint(self) -> str:
        """A stable hash of the content, to tell a retry from a reused batch id."""

        def text(value: Decimal | None) -> str | None:
            return format(value, "f") if value is not None else None

        canonical = {
            "source": self.source.value,
            "days": [
                [
                    d.activity_date.isoformat(),
                    d.steps,
                    text(d.distance_walking_running_m),
                    text(d.distance_cycling_m),
                    text(d.active_energy_kcal),
                ]
                for d in self.days
            ],
            "workouts": [
                [
                    w.external_id,
                    w.workout_type,
                    w.start.isoformat(),
                    w.end.isoformat(),
                    w.active_seconds,
                    text(w.distance_m),
                    text(w.active_energy_kcal),
                    w.origin_bundle_id,
                    w.origin_device,
                    w.was_user_entered,
                ]
                for w in self.workouts
            ],
            "deleted": list(self.deleted_workout_ids),
        }
        encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
