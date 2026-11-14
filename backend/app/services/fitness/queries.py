"""Read-only fitness views for the API. Nothing here writes or locks."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.timeutil import day_bounds_utc
from app.models.enums import WorkoutCategory
from app.models.fitness import FitnessDaily, FitnessSyncBatch, WorkoutEvent, WorkoutTypeRule
from app.models.rewards import DailyReward
from app.models.user import User
from app.services.fitness.ingestion import UNCLASSIFIED
from app.services.fitness.sources import SOURCE_PRIORITY, canonical_source, rewardable_sources


@dataclass(frozen=True)
class DayView:
    """One day as the user sees it: the facts, and what they earned."""

    activity_date: date
    source: str | None
    is_test_data: bool
    steps: int
    walking_distance_m: Decimal | None
    running_distance_m: Decimal | None
    cycling_distance_m: Decimal | None
    active_energy_kcal: Decimal | None
    eligible_minutes: int
    step_credits: int
    workout_credits: int
    entitled: int
    awarded: int
    capped: bool
    synced_at: datetime | None


@dataclass(frozen=True)
class WorkoutView:
    workout: WorkoutEvent
    category: str
    earns_minutes: bool
    reason: str  # why it does or does not earn minutes


@dataclass(frozen=True)
class SyncStatus:
    last_sync_at: datetime | None
    last_source: str | None
    batches: int
    days_stored: int
    workouts_stored: int


def _pick(rows: dict[str, FitnessDaily], settings: Settings) -> FitnessDaily | None:
    """The row to show for a day: the authoritative source, else whatever exists."""
    source = canonical_source(rows, settings)
    if source is not None:
        return rows[source.value]
    for candidate in SOURCE_PRIORITY:
        if candidate.value in rows:
            return rows[candidate.value]
    return next(iter(rows.values()), None)


async def days(
    session: AsyncSession, settings: Settings, user: User, first: date, last: date
) -> list[DayView]:
    """Every day in [first, last] that has activity or a reward, newest first."""
    daily: dict[date, dict[str, FitnessDaily]] = {}
    for row in (
        await session.scalars(
            select(FitnessDaily).where(
                FitnessDaily.user_id == user.id,
                FitnessDaily.activity_date >= first,
                FitnessDaily.activity_date <= last,
            )
        )
    ).all():
        daily.setdefault(row.activity_date, {})[row.source] = row

    rewards = {
        row.activity_date: row
        for row in (
            await session.scalars(
                select(DailyReward).where(
                    DailyReward.user_id == user.id,
                    DailyReward.activity_date >= first,
                    DailyReward.activity_date <= last,
                )
            )
        ).all()
    }

    views: list[DayView] = []
    for day in sorted({*daily, *rewards}, reverse=True):
        record = _pick(daily.get(day, {}), settings)
        reward = rewards.get(day)
        if record is None and (reward is None or reward.credits_awarded == 0):
            continue
        uncapped = (reward.step_credits + reward.workout_credits) if reward else 0
        views.append(
            DayView(
                activity_date=day,
                source=record.source if record else None,
                is_test_data=bool(record.is_test_data)
                if record
                else bool(reward and reward.is_test_data),
                steps=record.steps if record else 0,
                walking_distance_m=record.walking_distance_m if record else None,
                running_distance_m=record.running_distance_m if record else None,
                cycling_distance_m=record.cycling_distance_m if record else None,
                active_energy_kcal=record.active_energy_kcal if record else None,
                eligible_minutes=reward.minutes_counted if reward else 0,
                step_credits=reward.step_credits if reward else 0,
                workout_credits=reward.workout_credits if reward else 0,
                entitled=reward.entitled_credits if reward else 0,
                awarded=reward.credits_awarded if reward else 0,
                capped=bool(reward and reward.entitled_credits < uncapped),
                synced_at=record.synced_at if record else None,
            )
        )
    return views


def _reason(workout: WorkoutEvent, category: str, rewardable: set[str]) -> str:
    if workout.deleted_at is not None:
        return "deleted"
    if workout.excluded_from_rewards:
        return "excluded"
    if workout.duplicate_of is not None:
        return "duplicate"
    if not workout.verified:
        return "user_entered"
    if workout.source not in rewardable:
        return "source_not_rewardable"
    if category == WorkoutCategory.STEP_BASED.value:
        return "step_based"  # already paid through the step count
    if category == UNCLASSIFIED:
        return "unclassified_type"
    if category == WorkoutCategory.EXCLUDED.value:
        return "type_excluded"
    return "eligible"


async def workouts(
    session: AsyncSession,
    settings: Settings,
    user: User,
    first: date,
    last: date,
    *,
    include_deleted: bool = False,
) -> list[WorkoutView]:
    """Workouts starting on a local day in [first, last], newest first."""
    range_start, _ = day_bounds_utc(first, user.timezone)
    _, range_end = day_bounds_utc(last, user.timezone)
    query = (
        select(WorkoutEvent, WorkoutTypeRule.category)
        .outerjoin(WorkoutTypeRule, WorkoutTypeRule.workout_type == WorkoutEvent.workout_type)
        .where(
            WorkoutEvent.user_id == user.id,
            WorkoutEvent.start_time >= range_start,
            WorkoutEvent.start_time < range_end,
        )
        .order_by(WorkoutEvent.start_time.desc())
    )
    if not include_deleted:
        query = query.where(WorkoutEvent.deleted_at.is_(None))
    rewardable = {source.value for source in rewardable_sources(settings)}
    views: list[WorkoutView] = []
    for workout, stored_category in (await session.execute(query)).all():
        category = stored_category or UNCLASSIFIED
        reason = _reason(workout, category, rewardable)
        views.append(
            WorkoutView(
                workout=workout,
                category=category,
                earns_minutes=reason == "eligible",
                reason=reason,
            )
        )
    return views


async def workout_view(
    session: AsyncSession, settings: Settings, workout: WorkoutEvent
) -> WorkoutView:
    stored = await session.scalar(
        select(WorkoutTypeRule.category).where(WorkoutTypeRule.workout_type == workout.workout_type)
    )
    category = stored or UNCLASSIFIED
    rewardable = {source.value for source in rewardable_sources(settings)}
    reason = _reason(workout, category, rewardable)
    return WorkoutView(
        workout=workout, category=category, earns_minutes=reason == "eligible", reason=reason
    )


async def sync_status(session: AsyncSession, user_id: uuid.UUID) -> SyncStatus:
    latest = await session.scalar(
        select(FitnessSyncBatch)
        .where(FitnessSyncBatch.user_id == user_id)
        .order_by(FitnessSyncBatch.received_at.desc())
        .limit(1)
    )

    async def count(model: type[FitnessSyncBatch] | type[FitnessDaily] | type[WorkoutEvent]) -> int:
        total = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        return int(total or 0)

    return SyncStatus(
        last_sync_at=latest.received_at if latest else None,
        last_source=latest.source if latest else None,
        batches=await count(FitnessSyncBatch),
        days_stored=await count(FitnessDaily),
        workouts_stored=await count(WorkoutEvent),
    )


MAX_RANGE = timedelta(days=366)
