"""RewardEngine — turns a day's verified activity into credits, exactly once.

For one (user, activity date) it:

  1. takes the user's wallet row lock (all earning and spending for a user is serial);
  2. reads the day's facts from the authoritative source: the step total, and the
     verified, non-excluded, non-duplicate workouts that earn minutes;
  3. looks up the rules in force on that *activity date*;
  4. computes what the day is worth;
  5. pays only the difference between that and what the day has already been paid.

Step 5 is what makes the engine idempotent: repeating a sync, replaying a batch or
syncing the same day from two devices pays nothing extra. The amount already paid is a
high-water mark — a downward revision of the data does not claw credits back. Only an
explicit exclusion by the user reverses a reward, and never below a zero balance.

The engine never commits; the caller owns the transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.timeutil import day_bounds_utc, local_date
from app.models.enums import FitnessSource, WorkoutCategory
from app.models.fitness import FitnessDaily, WorkoutEvent, WorkoutTypeRule
from app.models.rewards import DailyReward
from app.models.user import User
from app.models.wallet import WalletTransaction
from app.services.fitness.sources import canonical_source, rewardable_sources
from app.services.rewards.calculator import (
    SECONDS_PER_MINUTE,
    WorkoutSpan,
    compute_entitlement,
    eligible_minutes,
)
from app.services.rewards.rules import rules_for_date
from app.services.wallet import WalletService

DAILY_REWARD_REFERENCE = "daily_reward"
TEST_DATA_LABEL = "[TEST DATA] "


def _active_seconds(duration_minutes: Decimal) -> int:
    """A stored duration (minutes, two decimals) as whole seconds, rounded down."""
    return int((duration_minutes * SECONDS_PER_MINUTE).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class DayReward:
    """The outcome of evaluating one day."""

    activity_date: date
    source: str | None  # authoritative source used; None if the day has no rewardable data
    is_test_data: bool
    steps: int
    eligible_minutes: int
    step_credits: int
    workout_credits: int
    entitled: int  # what the day is worth under its rules
    capped: bool
    awarded_before: int
    awarded_total: int  # what the day has been paid in total after this evaluation
    paid_now: int
    reversed_now: int
    in_reward_window: bool


class RewardEngine:
    def __init__(self, session: AsyncSession, *, settings: Settings, clock: Clock) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._wallets = WalletService(session, clock)

    def in_reward_window(self, user: User, activity_date: date) -> bool:
        """Whether late-arriving activity for this date can still earn credits."""
        today = local_date(self._clock.now(), user.timezone)
        age = (today - activity_date).days
        return 0 <= age <= self._settings.reward_window_days

    async def evaluate(
        self, user: User, activity_date: date, *, allow_reversal: bool = False
    ) -> DayReward:
        """Bring one day's reward up to date.

        ``allow_reversal`` is set only when the user has explicitly excluded activity:
        then a day that is now worth less than it was paid gives credits back, as far as
        the available balance allows.
        """
        await self._wallets.lock(user.id)
        in_window = self.in_reward_window(user, activity_date)

        source, steps = await self._steps(user, activity_date)
        minutes = await self._minutes(user, activity_date, source) if source else 0
        rules = await rules_for_date(self._session, activity_date)
        worth = compute_entitlement(steps, minutes, rules)
        is_test = source == FitnessSource.TEST.value

        row = await self._session.scalar(
            select(DailyReward)
            .where(DailyReward.user_id == user.id, DailyReward.activity_date == activity_date)
            .with_for_update()
        )
        now = self._clock.now()
        if row is None:
            row = DailyReward(
                user_id=user.id,
                activity_date=activity_date,
                credits_awarded=0,
                created_at=now,
                calculated_at=now,
            )
            self._session.add(row)
            await self._session.flush()

        awarded_before = row.credits_awarded
        paid = 0
        reversed_amount = 0
        label = TEST_DATA_LABEL if is_test else ""

        if in_window and worth.entitled > awarded_before:
            paid = worth.entitled - awarded_before
            await self._wallets.reward(
                user.id,
                paid,
                idempotency_key=await self._ledger_key(row.id),
                reference_type=DAILY_REWARD_REFERENCE,
                reference_id=row.id,
                description=f"{label}Activity reward for {activity_date.isoformat()}",
                metadata={
                    "activity_date": activity_date.isoformat(),
                    "steps": steps,
                    "eligible_minutes": minutes,
                    "entitled": worth.entitled,
                    "is_test_data": is_test,
                },
            )
            row.credits_awarded = worth.entitled
        elif allow_reversal and worth.entitled < awarded_before:
            reversal = await self._wallets.reverse_reward(
                user.id,
                awarded_before - worth.entitled,
                idempotency_key=await self._ledger_key(row.id),
                reference_type=DAILY_REWARD_REFERENCE,
                reference_id=row.id,
                description=f"{label}Reward reversed for {activity_date.isoformat()}",
            )
            if reversal is not None:
                reversed_amount = -reversal.amount
                row.credits_awarded = awarded_before - reversed_amount

        row.steps_counted = steps
        row.minutes_counted = minutes
        row.step_credits = worth.step_credits
        row.workout_credits = worth.workout_credits
        row.entitled_credits = worth.entitled
        row.rules_snapshot = rules.snapshot()
        row.is_test_data = is_test
        row.calculated_at = now
        await self._session.flush()

        return DayReward(
            activity_date=activity_date,
            source=source,
            is_test_data=is_test,
            steps=steps,
            eligible_minutes=minutes,
            step_credits=worth.step_credits,
            workout_credits=worth.workout_credits,
            entitled=worth.entitled,
            capped=worth.capped,
            awarded_before=awarded_before,
            awarded_total=row.credits_awarded,
            paid_now=paid,
            reversed_now=reversed_amount,
            in_reward_window=in_window,
        )

    # ------------------------------------------------------------------ facts

    async def _steps(self, user: User, activity_date: date) -> tuple[str | None, int]:
        """The authoritative source for the day and its step total.

        The source is the highest-priority rewardable source with any data for the day,
        whether that is a daily record or only workouts.
        """
        daily = {
            row.source: row
            for row in (
                await self._session.scalars(
                    select(FitnessDaily).where(
                        FitnessDaily.user_id == user.id,
                        FitnessDaily.activity_date == activity_date,
                    )
                )
            ).all()
        }
        day_start, day_end = day_bounds_utc(activity_date, user.timezone)
        workout_sources = (
            await self._session.scalars(
                select(WorkoutEvent.source)
                .where(*self._countable_workouts(user.id, day_start, day_end))
                .distinct()
            )
        ).all()
        source = canonical_source({*daily, *workout_sources}, self._settings)
        if source is None:
            return None, 0
        record = daily.get(source.value)
        return source.value, record.steps if record is not None else 0

    async def _minutes(self, user: User, activity_date: date, source: str) -> int:
        """Reward-eligible workout minutes for the day, from the authoritative source.

        Only workouts whose type is classified ``non_step`` earn minutes. Walking, running
        and hiking are ``step_based``: their effort is already in the step count.
        """
        day_start, day_end = day_bounds_utc(activity_date, user.timezone)
        rows = (
            await self._session.execute(
                select(
                    WorkoutEvent.start_time, WorkoutEvent.end_time, WorkoutEvent.duration_minutes
                )
                .join(WorkoutTypeRule, WorkoutTypeRule.workout_type == WorkoutEvent.workout_type)
                .where(
                    *self._countable_workouts(user.id, day_start, day_end),
                    WorkoutEvent.source == source,
                    WorkoutTypeRule.category == WorkoutCategory.NON_STEP.value,
                )
            )
        ).all()
        spans = [
            WorkoutSpan(start=start, end=end, active_seconds=_active_seconds(duration))
            for start, end, duration in rows
        ]
        return eligible_minutes(spans, day_start, day_end)

    def _countable_workouts(
        self, user_id: uuid.UUID, day_start: datetime, day_end: datetime
    ) -> tuple[ColumnElement[bool], ...]:
        """Filter: this user's workouts touching the day that are allowed to count."""
        earliest_start = day_start - timedelta(hours=self._settings.max_workout_hours)
        sources = [s.value for s in rewardable_sources(self._settings)]
        return (
            WorkoutEvent.user_id == user_id,
            WorkoutEvent.start_time >= earliest_start,  # bounded: keeps the index usable
            WorkoutEvent.start_time < day_end,
            WorkoutEvent.end_time > day_start,
            WorkoutEvent.source.in_(sources),
            WorkoutEvent.verified.is_(True),
            WorkoutEvent.excluded_from_rewards.is_(False),
            WorkoutEvent.duplicate_of.is_(None),
            WorkoutEvent.deleted_at.is_(None),
        )

    async def _ledger_key(self, daily_reward_id: uuid.UUID) -> str:
        """A fresh idempotency key for the next ledger entry of this day.

        Safe because the caller holds the wallet lock: no other transaction can be
        writing a ledger row for this user, so the count cannot change underneath us.
        """
        entries = await self._session.scalar(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.reference_type == DAILY_REWARD_REFERENCE,
                WalletTransaction.reference_id == daily_reward_id,
            )
        )
        return f"reward:{daily_reward_id}:{int(entries or 0)}"
