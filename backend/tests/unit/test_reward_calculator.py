"""The reward formula: the specification's examples, edge cases, and properties that
must hold for *any* input (checked with generated data)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.timeutil import day_bounds_utc
from app.services.rewards.calculator import (
    Entitlement,
    RuleSet,
    WorkoutSpan,
    compute_entitlement,
    eligible_minutes,
    whole_seconds,
)

# The initial configuration: 1,000 steps = 100 WC; 30 minutes = 150 WC; 1,500 WC a day.
INITIAL = RuleSet(steps_rate=Decimal("0.1"), minutes_rate=Decimal("5"), daily_cap=1500)

DAY = datetime(2026, 11, 12, tzinfo=UTC)
NEXT_DAY = DAY + timedelta(days=1)


def at(hour: int, minute: int = 0, *, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


def span(start: datetime, end: datetime, active_minutes: int | None = None) -> WorkoutSpan:
    seconds = whole_seconds(end - start) if active_minutes is None else active_minutes * 60
    return WorkoutSpan(start=start, end=end, active_seconds=seconds)


# ------------------------------------------------------------------ entitlement


@pytest.mark.parametrize(
    ("steps", "minutes", "expected"),
    [
        (1_000, 0, 100),  # the reward table from the specification
        (5_000, 0, 500),
        (10_000, 0, 1_000),
        (0, 30, 150),  # 30 minutes of strength training or cycling
        (8_000, 30, 950),  # the worked example: 800 + 150
        (0, 0, 0),
        (9, 0, 0),  # rounds down: nine steps earn nothing
        (19, 0, 1),
        (12_345, 0, 1_234),
        (15_000, 0, 1_500),  # exactly at the cap
        (15_001, 0, 1_500),
        (20_000, 60, 1_500),  # far above: capped
        (14_000, 30, 1_500),  # 1,400 + 150 = 1,550 -> capped
        (0, 300, 1_500),  # workouts alone can reach the cap, not pass it
    ],
)
def test_specification_examples(steps: int, minutes: int, expected: int) -> None:
    assert compute_entitlement(steps, minutes, INITIAL).entitled == expected


def test_components_are_reported_separately() -> None:
    result = compute_entitlement(8_000, 30, INITIAL)
    assert result == Entitlement(step_credits=800, workout_credits=150, uncapped=950, entitled=950)
    assert not result.capped
    capped = compute_entitlement(14_000, 30, INITIAL)
    assert (capped.uncapped, capped.entitled, capped.capped) == (1_550, 1_500, True)


def test_per_activity_limits_apply_before_the_daily_cap() -> None:
    rules = RuleSet(
        steps_rate=Decimal("0.1"),
        steps_limit=600,
        minutes_rate=Decimal("5"),
        minutes_limit=200,
        daily_cap=700,
    )
    result = compute_entitlement(10_000, 60, rules)
    assert (result.step_credits, result.workout_credits) == (600, 200)
    assert (result.uncapped, result.entitled) == (800, 700)


def test_a_missing_rule_earns_nothing_and_a_missing_cap_does_not_cap() -> None:
    assert compute_entitlement(10_000, 30, RuleSet()).entitled == 0
    steps_only = RuleSet(steps_rate=Decimal("0.1"))
    assert compute_entitlement(50_000, 30, steps_only).entitled == 5_000


def test_fractional_rates_round_down_on_the_total_not_per_step() -> None:
    rules = RuleSet(steps_rate=Decimal("0.15"), minutes_rate=Decimal("2.5"))
    result = compute_entitlement(1_001, 3, rules)
    assert (result.step_credits, result.workout_credits) == (150, 7)  # 150.15 and 7.5


def test_negative_activity_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        compute_entitlement(-1, 0, INITIAL)
    with pytest.raises(ValueError, match="cannot be negative"):
        compute_entitlement(0, -1, INITIAL)


def test_rule_snapshot_is_json_safe_and_exact() -> None:
    snapshot = INITIAL.snapshot()
    assert snapshot["steps"]["rate"] == "0.1"
    assert snapshot["workout_minutes"]["rate"] == "5"
    assert snapshot["daily_total"]["limit"] == 1500
    assert RuleSet().snapshot()["steps"] == {"rule_id": None, "rate": None, "limit": None}


# ------------------------------------------------------------- eligible minutes


def test_a_single_workout_counts_its_active_minutes() -> None:
    assert eligible_minutes([span(at(18), at(18, 30))], DAY, NEXT_DAY) == 30
    assert eligible_minutes([], DAY, NEXT_DAY) == 0


def test_paused_time_is_not_counted() -> None:
    """45 minutes on the clock, 30 of them active."""
    assert eligible_minutes([span(at(18), at(18, 45), active_minutes=30)], DAY, NEXT_DAY) == 30


def test_active_time_cannot_exceed_the_time_that_passed() -> None:
    lying = span(at(18), at(18, 20), active_minutes=600)
    assert eligible_minutes([lying], DAY, NEXT_DAY) == 20


def test_separate_workouts_add_up() -> None:
    workouts = [span(at(7), at(7, 30)), span(at(18), at(18, 45))]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 75


def test_identical_workouts_are_counted_once() -> None:
    """The same session reported twice (two apps, two ids) is still one session."""
    workout = span(at(18), at(18, 30))
    assert eligible_minutes([workout, workout, workout], DAY, NEXT_DAY) == 30


def test_overlapping_workouts_never_count_a_minute_twice() -> None:
    # 18:00-18:40 and 18:20-19:00 overlap by 20 minutes: 60 minutes passed, not 80.
    workouts = [span(at(18), at(18, 40)), span(at(18, 20), at(19))]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 60


def test_a_workout_contained_in_another_adds_nothing() -> None:
    workouts = [span(at(18), at(19)), span(at(18, 15), at(18, 30))]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 60


def test_adjacent_workouts_are_not_overlapping() -> None:
    workouts = [span(at(18), at(18, 30)), span(at(18, 30), at(19))]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 60


def test_overlap_with_pauses_is_bounded_by_both_active_time_and_elapsed_time() -> None:
    # Two overlapping 60-minute windows covering 90 minutes, each only 20 minutes active.
    workouts = [
        span(at(18), at(19), active_minutes=20),
        span(at(18, 30), at(19, 30), active_minutes=20),
    ]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 40


def test_a_workout_across_midnight_is_split_between_the_two_days() -> None:
    late = span(at(23, 40), at(0, 20, day=1))  # 40 minutes: 20 before midnight, 20 after
    assert eligible_minutes([late], DAY, NEXT_DAY) == 20
    assert eligible_minutes([late], NEXT_DAY, NEXT_DAY + timedelta(days=1)) == 20


def test_a_paused_workout_across_midnight_is_split_proportionally() -> None:
    # 60 minutes on the clock (45 before midnight), 40 active: 30 and 10.
    late = span(at(23, 15), at(0, 15, day=1), active_minutes=40)
    assert eligible_minutes([late], DAY, NEXT_DAY) == 30
    assert eligible_minutes([late], NEXT_DAY, NEXT_DAY + timedelta(days=1)) == 10


def test_workouts_on_other_days_do_not_count() -> None:
    yesterday = span(at(18, day=-1), at(19, day=-1))
    tomorrow = span(at(6, day=1), at(7, day=1))
    assert eligible_minutes([yesterday, tomorrow], DAY, NEXT_DAY) == 0


def test_partial_minutes_round_down_on_the_day_total() -> None:
    # 10:59 + 10:59 = 21:58 -> 21 minutes (not 10 + 10).
    workouts = [
        WorkoutSpan(at(7), at(7, 11), active_seconds=659),
        WorkoutSpan(at(9), at(9, 11), active_seconds=659),
    ]
    assert eligible_minutes(workouts, DAY, NEXT_DAY) == 21


def test_degenerate_spans_are_ignored() -> None:
    backwards = WorkoutSpan(at(19), at(18), active_seconds=600)
    empty = WorkoutSpan(at(18), at(18), active_seconds=0)
    zero_active = WorkoutSpan(at(18), at(19), active_seconds=0)
    assert eligible_minutes([backwards, empty, zero_active], DAY, NEXT_DAY) == 0
    with pytest.raises(ValueError, match="day_end must be after day_start"):
        eligible_minutes([], NEXT_DAY, DAY)


def test_lisbon_days_use_local_midnight_including_daylight_saving() -> None:
    """A summer workout at 23:30-00:30 local time straddles Lisbon's midnight, which is
    23:00 UTC — not UTC's."""
    start, end = day_bounds_utc(datetime(2026, 7, 14, tzinfo=UTC).date(), "Europe/Lisbon")
    next_start, next_end = day_bounds_utc(datetime(2026, 7, 15, tzinfo=UTC).date(), "Europe/Lisbon")
    workout = span(  # 23:30 WEST on the 14th to 00:30 WEST on the 15th
        datetime(2026, 7, 14, 22, 30, tzinfo=UTC), datetime(2026, 7, 14, 23, 30, tzinfo=UTC)
    )
    assert eligible_minutes([workout], start, end) == 30
    assert eligible_minutes([workout], next_start, next_end) == 30

    # The 25-hour day (clocks go back): a workout through the repeated hour is not lost.
    long_start, long_end = day_bounds_utc(
        datetime(2026, 10, 25, tzinfo=UTC).date(), "Europe/Lisbon"
    )
    assert whole_seconds(long_end - long_start) == 25 * 3600
    through_the_change = span(
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    )
    assert eligible_minutes([through_the_change], long_start, long_end) == 90


# ------------------------------------------------------------------- properties

rates = st.decimals(min_value=0, max_value=50, places=4, allow_nan=False, allow_infinity=False)
optional_limits = st.one_of(st.none(), st.integers(min_value=0, max_value=5_000))
rule_sets = st.builds(
    RuleSet,
    steps_rate=st.one_of(st.none(), rates),
    steps_limit=optional_limits,
    minutes_rate=st.one_of(st.none(), rates),
    minutes_limit=optional_limits,
    daily_cap=optional_limits,
)
step_counts = st.integers(min_value=0, max_value=200_000)
minute_counts = st.integers(min_value=0, max_value=1_440)


@given(steps=step_counts, minutes=minute_counts, rules=rule_sets)
def test_entitlement_is_never_negative_and_never_exceeds_any_cap(
    steps: int, minutes: int, rules: RuleSet
) -> None:
    result = compute_entitlement(steps, minutes, rules)
    assert result.entitled >= 0
    assert result.entitled <= result.uncapped
    assert result.uncapped == result.step_credits + result.workout_credits
    if rules.daily_cap is not None:
        assert result.entitled <= rules.daily_cap
    if rules.steps_limit is not None:
        assert result.step_credits <= rules.steps_limit
    if rules.minutes_limit is not None:
        assert result.workout_credits <= rules.minutes_limit
    for value in (result.step_credits, result.workout_credits, result.uncapped, result.entitled):
        assert type(value) is int  # credits are integers, always


@given(steps=step_counts, minutes=minute_counts, more=st.integers(0, 50_000), rules=rule_sets)
def test_more_activity_never_earns_less(
    steps: int, minutes: int, more: int, rules: RuleSet
) -> None:
    base = compute_entitlement(steps, minutes, rules).entitled
    assert compute_entitlement(steps + more, minutes, rules).entitled >= base
    assert compute_entitlement(steps, minutes + more % 600, rules).entitled >= base


@given(steps=step_counts, minutes=minute_counts, rules=rule_sets)
def test_entitlement_is_deterministic(steps: int, minutes: int, rules: RuleSet) -> None:
    assert compute_entitlement(steps, minutes, rules) == compute_entitlement(steps, minutes, rules)


@st.composite
def workout_spans(draw: st.DrawFn) -> WorkoutSpan:
    """Workouts anywhere from the evening before to the morning after the day."""
    start = DAY + timedelta(seconds=draw(st.integers(-6 * 3600, 30 * 3600)))
    length = draw(st.integers(1, 6 * 3600))
    active = draw(st.integers(0, length + 600))  # sometimes more than elapsed: must be clamped
    return WorkoutSpan(start=start, end=start + timedelta(seconds=length), active_seconds=active)


span_lists = st.lists(workout_spans(), max_size=8)


@given(spans=span_lists)
def test_minutes_never_exceed_the_day_or_the_time_that_actually_passed(
    spans: list[WorkoutSpan],
) -> None:
    minutes = eligible_minutes(spans, DAY, NEXT_DAY)
    assert 0 <= minutes <= 24 * 60
    claimed = sum(min(s.active_seconds, whole_seconds(s.end - s.start)) for s in spans)
    assert minutes * 60 <= claimed


@given(spans=span_lists)
def test_reporting_a_workout_twice_never_adds_minutes(spans: list[WorkoutSpan]) -> None:
    """Duplicates — the same workout arriving from two apps — are worth nothing extra
    beyond what the union of intervals allows."""
    once = eligible_minutes(spans, DAY, NEXT_DAY)
    elapsed_minutes = _union_seconds(spans) // 60
    twice = eligible_minutes(spans + spans, DAY, NEXT_DAY)
    assert twice <= elapsed_minutes
    assert twice >= once


@given(spans=span_lists, seed=st.randoms(use_true_random=False))
def test_the_order_workouts_arrive_in_does_not_matter(
    spans: list[WorkoutSpan], seed: object
) -> None:
    shuffled = list(spans)
    seed.shuffle(shuffled)  # type: ignore[attr-defined]
    assert eligible_minutes(shuffled, DAY, NEXT_DAY) == eligible_minutes(spans, DAY, NEXT_DAY)


@given(spans=span_lists, extra=workout_spans())
def test_an_extra_workout_never_reduces_the_minutes(
    spans: list[WorkoutSpan], extra: WorkoutSpan
) -> None:
    assert eligible_minutes([*spans, extra], DAY, NEXT_DAY) >= eligible_minutes(
        spans, DAY, NEXT_DAY
    )


@given(workout=workout_spans())
def test_splitting_at_midnight_loses_at_most_rounding(workout: WorkoutSpan) -> None:
    """Across the three days a workout can touch, its minutes add up to its active time,
    give or take the per-day rounding down."""
    days = [DAY + timedelta(days=offset) for offset in (-1, 0, 1, 2)]
    per_day = [eligible_minutes([workout], start, end) for start, end in pairwise(days)]
    whole = min(workout.active_seconds, whole_seconds(workout.end - workout.start)) // 60
    assert whole - 3 <= sum(per_day) <= whole


def _union_seconds(spans: list[WorkoutSpan]) -> int:
    total = 0
    cursor: datetime | None = None
    for start, end in sorted(
        (max(s.start, DAY), min(s.end, NEXT_DAY)) for s in spans if s.active_seconds > 0
    ):
        if end <= start:
            continue
        if cursor is None or start > cursor:
            total += whole_seconds(end - start)
            cursor = end
        elif end > cursor:
            total += whole_seconds(end - cursor)
            cursor = end
    return total
