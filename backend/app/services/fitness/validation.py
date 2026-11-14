"""Plausibility checks for incoming fitness records. Pure functions.

The server does not trust the device. A record that is impossible, in the future, too
old or malformed is rejected with a stable reason code and is never stored — and a
rejected record never blocks the rest of the batch.

This is validation, not proof: it cannot show that exercise happened, only that a claim
is not absurd. See docs/SECURITY.md ("Honest limits").
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.core.config import Settings
from app.core.timeutil import local_date
from app.services.fitness.model import VALID_WORKOUT_TYPE, DayRecord, WorkoutRecord
from app.services.rewards.calculator import whole_seconds

METRES_PER_KILOMETRE = 1000
SECONDS_PER_HOUR = 3600
MAX_ACTIVE_ENERGY_KCAL = Decimal(20_000)

# Device clocks drift, and a workout's reported active time can be a few seconds longer
# than its start-to-end span. Tolerate that much, no more.
CLOCK_SKEW = timedelta(minutes=5)
DURATION_TOLERANCE_SECONDS = 60


@dataclass(frozen=True)
class Bounds:
    max_steps_per_day: int
    max_distance_m_per_day: Decimal
    max_workout_seconds: int
    sync_window_days: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "Bounds":
        return cls(
            max_steps_per_day=settings.max_steps_per_day,
            max_distance_m_per_day=Decimal(settings.max_distance_km_per_day * METRES_PER_KILOMETRE),
            max_workout_seconds=settings.max_workout_hours * SECONDS_PER_HOUR,
            sync_window_days=settings.sync_window_days,
        )


@dataclass(frozen=True)
class Rejection:
    kind: Literal["day", "workout"]
    reference: str  # the date, or the workout's external id
    code: str  # stable, machine-readable
    detail: str


def _out_of_range(value: Decimal | None, maximum: Decimal) -> bool:
    return value is not None and (value < 0 or value > maximum)


def validate_day(day: DayRecord, *, today: date, bounds: Bounds) -> Rejection | None:
    """None if the day is acceptable, otherwise why it is not."""
    reference = day.activity_date.isoformat()

    def reject(code: str, detail: str) -> Rejection:
        return Rejection(kind="day", reference=reference, code=code, detail=detail)

    if day.activity_date > today:
        return reject("date_in_future", "The date has not started yet in your timezone.")
    if (today - day.activity_date).days > bounds.sync_window_days:
        return reject(
            "too_old", f"Activity older than {bounds.sync_window_days} days is not accepted."
        )
    if day.steps < 0 or day.steps > bounds.max_steps_per_day:
        return reject(
            "implausible_steps",
            f"Step counts must be between 0 and {bounds.max_steps_per_day}.",
        )
    if _out_of_range(day.distance_walking_running_m, bounds.max_distance_m_per_day) or (
        _out_of_range(day.distance_cycling_m, bounds.max_distance_m_per_day)
    ):
        return reject("implausible_distance", "A distance is negative or implausibly large.")
    if _out_of_range(day.active_energy_kcal, MAX_ACTIVE_ENERGY_KCAL):
        return reject("implausible_energy", "Active energy is negative or implausibly large.")
    return None


def validate_workout(
    workout: WorkoutRecord, *, now: datetime, timezone: str, bounds: Bounds
) -> Rejection | None:
    """None if the workout is acceptable, otherwise why it is not."""

    def reject(code: str, detail: str) -> Rejection:
        return Rejection(kind="workout", reference=workout.external_id, code=code, detail=detail)

    if not VALID_WORKOUT_TYPE.match(workout.workout_type):
        return reject("invalid_type", "The workout type is not a recognisable name.")
    if workout.end <= workout.start:
        return reject("invalid_interval", "A workout must end after it starts.")
    if workout.end > now + CLOCK_SKEW:
        return reject("in_future", "The workout ends in the future.")

    span_seconds = whole_seconds(workout.end - workout.start)
    if span_seconds > bounds.max_workout_seconds:
        hours = bounds.max_workout_seconds // SECONDS_PER_HOUR
        return reject("too_long", f"Workouts longer than {hours} hours are not accepted.")
    if workout.active_seconds < 0:
        return reject("invalid_duration", "Active time cannot be negative.")
    if workout.active_seconds > span_seconds + DURATION_TOLERANCE_SECONDS:
        return reject(
            "duration_exceeds_interval",
            "Active time is longer than the time between start and end.",
        )

    today = local_date(now, timezone)
    if (today - local_date(workout.start, timezone)).days > bounds.sync_window_days:
        return reject(
            "too_old", f"Activity older than {bounds.sync_window_days} days is not accepted."
        )
    if _out_of_range(workout.distance_m, bounds.max_distance_m_per_day):
        return reject("implausible_distance", "The distance is negative or implausibly large.")
    if _out_of_range(workout.active_energy_kcal, MAX_ACTIVE_ENERGY_KCAL):
        return reject("implausible_energy", "Active energy is negative or implausibly large.")
    return None
