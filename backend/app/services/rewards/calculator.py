"""The reward formula, as pure functions.

    WC(day) = min(daily_cap, floor(r_steps × S) + floor(r_minutes × M))

No database, no clock, no I/O: given the same inputs these functions always return the
same outputs, which is what lets the engine recompute a day any number of times and pay
only the difference.

Arithmetic is integers and ``Decimal`` throughout. Durations are handled in whole
seconds; nothing here ever becomes a float.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

_ONE_SECOND = timedelta(seconds=1)
SECONDS_PER_MINUTE = 60


def whole_seconds(delta: timedelta) -> int:
    """A timedelta in whole seconds, without passing through a float."""
    return delta // _ONE_SECOND


@dataclass(frozen=True)
class WorkoutSpan:
    """The part of a workout that matters for rewards: when, and how long it was active.

    ``active_seconds`` can be less than the wall-clock span (the watch was paused).
    """

    start: datetime
    end: datetime
    active_seconds: int


@dataclass(frozen=True)
class RuleSet:
    """The reward rules in force for one activity date. A missing rate earns nothing."""

    steps_rate: Decimal | None = None  # WC per step
    steps_limit: int | None = None  # optional cap on step credits
    minutes_rate: Decimal | None = None  # WC per eligible workout minute
    minutes_limit: int | None = None  # optional cap on workout credits
    daily_cap: int | None = None  # cap on the day's total
    steps_rule_id: uuid.UUID | None = None
    minutes_rule_id: uuid.UUID | None = None
    cap_rule_id: uuid.UUID | None = None

    def snapshot(self) -> dict[str, dict[str, str | int | None]]:
        """What was applied, for the reward ledger. Decimals as strings."""
        return {
            "steps": {
                "rule_id": str(self.steps_rule_id) if self.steps_rule_id else None,
                "rate": format(self.steps_rate, "f") if self.steps_rate is not None else None,
                "limit": self.steps_limit,
            },
            "workout_minutes": {
                "rule_id": str(self.minutes_rule_id) if self.minutes_rule_id else None,
                "rate": format(self.minutes_rate, "f") if self.minutes_rate is not None else None,
                "limit": self.minutes_limit,
            },
            "daily_total": {
                "rule_id": str(self.cap_rule_id) if self.cap_rule_id else None,
                "limit": self.daily_cap,
            },
        }


@dataclass(frozen=True)
class Entitlement:
    step_credits: int
    workout_credits: int
    uncapped: int
    entitled: int

    @property
    def capped(self) -> bool:
        return self.entitled < self.uncapped


def eligible_minutes(spans: Iterable[WorkoutSpan], day_start: datetime, day_end: datetime) -> int:
    """Whole reward-eligible workout minutes that fall inside [day_start, day_end).

    Two rules keep a minute from ever being counted twice:

    * a workout that crosses the day boundary contributes only the share of its active
      time that lies inside the day (proportional to wall-clock overlap);
    * the total is bounded by the length of the *union* of the workouts' intervals, so
      overlapping or duplicated workouts cannot add up to more time than actually passed.
    """
    if day_end <= day_start:
        raise ValueError("day_end must be after day_start")

    active_total = 0
    clipped: list[tuple[datetime, datetime]] = []
    for span in spans:
        span_seconds = whole_seconds(span.end - span.start)
        if span_seconds <= 0 or span.active_seconds <= 0:
            continue
        start = max(span.start, day_start)
        end = min(span.end, day_end)
        overlap_seconds = whole_seconds(end - start)
        if overlap_seconds <= 0:
            continue
        active = min(span.active_seconds, span_seconds)
        active_total += active * overlap_seconds // span_seconds
        clipped.append((start, end))

    union_total = 0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in sorted(clipped):
        if current_end is None or current_start is None or start > current_end:
            if current_end is not None and current_start is not None:
                union_total += whole_seconds(current_end - current_start)
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_end is not None and current_start is not None:
        union_total += whole_seconds(current_end - current_start)

    return min(active_total, union_total) // SECONDS_PER_MINUTE


def _credits(rate: Decimal | None, quantity: int, limit: int | None) -> int:
    if rate is None or quantity <= 0:
        return 0
    earned = int((rate * quantity).to_integral_value(rounding=ROUND_FLOOR))
    return min(earned, limit) if limit is not None else earned


def compute_entitlement(steps: int, minutes: int, rules: RuleSet) -> Entitlement:
    """What a day is worth. Rounds down; never exceeds the daily cap."""
    if steps < 0 or minutes < 0:
        raise ValueError("steps and minutes cannot be negative")
    step_credits = _credits(rules.steps_rate, steps, rules.steps_limit)
    workout_credits = _credits(rules.minutes_rate, minutes, rules.minutes_limit)
    uncapped = step_credits + workout_credits
    entitled = min(uncapped, rules.daily_cap) if rules.daily_cap is not None else uncapped
    return Entitlement(
        step_credits=step_credits,
        workout_credits=workout_credits,
        uncapped=uncapped,
        entitled=entitled,
    )
