"""Normalised fitness records, plausibility validation and source policy (no database)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.enums import FitnessSource
from app.services.fitness.model import (
    DayRecord,
    SyncBatch,
    WorkoutRecord,
    normalise_workout_type,
)
from app.services.fitness.sources import SOURCE_PRIORITY, canonical_source, rewardable_sources
from app.services.fitness.validation import Bounds, validate_day, validate_workout

LISBON = "Europe/Lisbon"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 13)
BOUNDS = Bounds(
    max_steps_per_day=100_000,
    max_distance_m_per_day=Decimal(500_000),
    max_workout_seconds=12 * 3600,
    sync_window_days=30,
)


def settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret=SecretStr("0123456789abcdef" * 4),
        postgres_password=SecretStr("a-strong-local-password"),
        **overrides,
    )


def a_workout(**overrides: Any) -> WorkoutRecord:
    values: dict[str, Any] = {
        "external_id": "HK-1",
        "workout_type": "cycling",
        "start": NOW - timedelta(hours=3),
        "end": NOW - timedelta(hours=2),
        "active_seconds": 3000,
    }
    values.update(overrides)
    return WorkoutRecord(**values)


# ---------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cycling", "cycling"),
        ("Cycling", "cycling"),
        ("traditionalStrengthTraining", "traditional_strength_training"),
        ("HKWorkoutActivityTypeTraditionalStrengthTraining", "traditional_strength_training"),
        ("Traditional Strength Training", "traditional_strength_training"),
        ("traditional_strength_training", "traditional_strength_training"),
        ("highIntensityIntervalTraining", "high_intensity_interval_training"),
        ("  running  ", "running"),
        ("Stair-Climbing", "stair_climbing"),
        ("mixedCardio2", "mixed_cardio2"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_workout_types_are_normalised_from_any_spelling(raw: str, expected: str) -> None:
    assert normalise_workout_type(raw) == expected


def test_a_workout_knows_which_local_days_it_touches() -> None:
    evening = a_workout(
        start=datetime(2026, 11, 12, 18, 0, tzinfo=UTC),
        end=datetime(2026, 11, 12, 19, 0, tzinfo=UTC),
    )
    across_midnight = a_workout(
        start=datetime(2026, 11, 12, 23, 40, tzinfo=UTC),
        end=datetime(2026, 11, 13, 0, 20, tzinfo=UTC),
    )
    ends_at_midnight = a_workout(
        start=datetime(2026, 11, 12, 23, 0, tzinfo=UTC),
        end=datetime(2026, 11, 13, 0, 0, tzinfo=UTC),
    )
    # 22:30-23:30 UTC in July is 23:30-00:30 in Lisbon: two local days, one UTC day.
    summer_night = a_workout(
        start=datetime(2026, 7, 14, 22, 30, tzinfo=UTC),
        end=datetime(2026, 7, 14, 23, 30, tzinfo=UTC),
    )

    assert evening.local_dates(LISBON) == (date(2026, 11, 12),)
    assert across_midnight.local_dates(LISBON) == (date(2026, 11, 12), date(2026, 11, 13))
    assert ends_at_midnight.local_dates(LISBON) == (date(2026, 11, 12),)
    assert summer_night.local_dates(LISBON) == (date(2026, 7, 14), date(2026, 7, 15))
    assert summer_night.local_dates("UTC") == (date(2026, 7, 14),)


def test_batch_fingerprint_identifies_content_not_the_batch_id() -> None:
    def batch(batch_id: uuid.UUID, steps: int = 8000, **workout_changes: Any) -> SyncBatch:
        return SyncBatch(
            batch_id=batch_id,
            source=FitnessSource.APPLE_HEALTHKIT,
            days=(DayRecord(activity_date=TODAY, steps=steps),),
            workouts=(a_workout(**workout_changes),),
        )

    reference = batch(uuid.uuid4()).fingerprint()
    assert len(reference) == 64
    assert batch(uuid.uuid4()).fingerprint() == reference  # same content, another id
    assert batch(uuid.uuid4(), steps=8001).fingerprint() != reference
    assert batch(uuid.uuid4(), active_seconds=3001).fingerprint() != reference
    assert batch(uuid.uuid4(), was_user_entered=True).fingerprint() != reference
    other_source = SyncBatch(
        batch_id=uuid.uuid4(),
        source=FitnessSource.TEST,
        days=(DayRecord(activity_date=TODAY, steps=8000),),
        workouts=(a_workout(),),
    )
    assert other_source.fingerprint() != reference


# ------------------------------------------------------------------- validation


def test_a_plausible_day_is_accepted() -> None:
    record = DayRecord(
        activity_date=TODAY,
        steps=12_345,
        distance_walking_running_m=Decimal("9100.50"),
        distance_cycling_m=Decimal("25000"),
        active_energy_kcal=Decimal("640"),
    )
    assert validate_day(record, today=TODAY, bounds=BOUNDS) is None
    oldest = DayRecord(activity_date=TODAY - timedelta(days=30), steps=0)
    assert validate_day(oldest, today=TODAY, bounds=BOUNDS) is None
    assert validate_day(DayRecord(TODAY, 100_000), today=TODAY, bounds=BOUNDS) is None


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (DayRecord(TODAY + timedelta(days=1), 5000), "date_in_future"),
        (DayRecord(TODAY - timedelta(days=31), 5000), "too_old"),
        (DayRecord(TODAY, 100_001), "implausible_steps"),
        (DayRecord(TODAY, -1), "implausible_steps"),
        (
            DayRecord(TODAY, 5000, distance_walking_running_m=Decimal("500001")),
            "implausible_distance",
        ),
        (DayRecord(TODAY, 5000, distance_cycling_m=Decimal("-1")), "implausible_distance"),
        (DayRecord(TODAY, 5000, active_energy_kcal=Decimal("20001")), "implausible_energy"),
    ],
)  # fmt: skip
def test_implausible_days_are_rejected_with_a_reason(record: DayRecord, code: str) -> None:
    rejection = validate_day(record, today=TODAY, bounds=BOUNDS)
    assert rejection is not None
    assert (rejection.kind, rejection.code) == ("day", code)
    assert rejection.reference == record.activity_date.isoformat()
    assert rejection.detail


def test_a_plausible_workout_is_accepted() -> None:
    assert validate_workout(a_workout(), now=NOW, timezone=LISBON, bounds=BOUNDS) is None
    # A few seconds of active time beyond the span, and a device clock slightly ahead,
    # are tolerated.
    slack = a_workout(active_seconds=3600 + 60)
    just_ended = a_workout(
        start=NOW - timedelta(minutes=30), end=NOW + timedelta(minutes=4), active_seconds=1800
    )
    longest = a_workout(start=NOW - timedelta(hours=13), end=NOW - timedelta(hours=1))
    for workout in (slack, just_ended, longest):
        assert validate_workout(workout, now=NOW, timezone=LISBON, bounds=BOUNDS) is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"workout_type": ""}, "invalid_type"),
        ({"workout_type": "Not Normalised"}, "invalid_type"),
        ({"workout_type": "x" * 65}, "invalid_type"),
        ({"end": NOW - timedelta(hours=3)}, "invalid_interval"),  # ends when it starts
        ({"end": NOW - timedelta(hours=4)}, "invalid_interval"),
        ({"start": NOW, "end": NOW + timedelta(minutes=6)}, "in_future"),
        ({"start": NOW - timedelta(hours=13), "end": NOW - timedelta(minutes=59)}, "too_long"),
        ({"active_seconds": -1}, "invalid_duration"),
        ({"active_seconds": 3600 + 61}, "duration_exceeds_interval"),
        (
            {"start": NOW - timedelta(days=31, hours=1), "end": NOW - timedelta(days=31)},
            "too_old",
        ),
        ({"distance_m": Decimal("500001")}, "implausible_distance"),
        ({"active_energy_kcal": Decimal("-0.01")}, "implausible_energy"),
    ],
)
def test_implausible_workouts_are_rejected_with_a_reason(
    changes: dict[str, Any], code: str
) -> None:
    rejection = validate_workout(a_workout(**changes), now=NOW, timezone=LISBON, bounds=BOUNDS)
    assert rejection is not None
    assert (rejection.kind, rejection.reference, rejection.code) == ("workout", "HK-1", code)


def test_bounds_come_from_configuration() -> None:
    bounds = Bounds.from_settings(
        settings(max_steps_per_day=60_000, max_distance_km_per_day=120, max_workout_hours=6)
    )
    assert bounds.max_steps_per_day == 60_000
    assert bounds.max_distance_m_per_day == Decimal(120_000)
    assert bounds.max_workout_seconds == 6 * 3600
    assert validate_day(DayRecord(TODAY, 60_001), today=TODAY, bounds=bounds) is not None


def test_the_reward_window_cannot_exceed_the_sync_window() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="REWARD_WINDOW_DAYS cannot exceed"):
        settings(reward_window_days=40, sync_window_days=30)


# ---------------------------------------------------------------- source policy


def test_test_data_can_earn_only_where_it_is_enabled() -> None:
    assert rewardable_sources(settings()) == (FitnessSource.APPLE_HEALTHKIT,)
    assert rewardable_sources(settings(allow_test_fitness_data=True)) == (
        FitnessSource.APPLE_HEALTHKIT,
        FitnessSource.TEST,
    )
    assert FitnessSource.MANUAL not in rewardable_sources(settings(allow_test_fitness_data=True))


def test_a_day_has_exactly_one_authoritative_source() -> None:
    lenient = settings(allow_test_fitness_data=True)
    strict = settings()

    assert canonical_source({"apple_healthkit", "test"}, lenient) is FitnessSource.APPLE_HEALTHKIT
    assert canonical_source({"test"}, lenient) is FitnessSource.TEST
    assert canonical_source({"test"}, strict) is None  # stored test rows earn nothing here
    assert canonical_source({"manual"}, lenient) is None
    assert canonical_source(set(), lenient) is None
    assert SOURCE_PRIORITY[0] is FitnessSource.APPLE_HEALTHKIT
