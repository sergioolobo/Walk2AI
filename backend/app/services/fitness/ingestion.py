"""Fitness ingestion: validate, store, de-duplicate, then let the reward engine pay.

One synchronisation is one database transaction, taken under the user's wallet lock:

  * a batch id that was already processed returns the stored result (a retried upload
    is harmless); the same id with different content is refused;
  * each day is upserted — the latest values for a (user, date, source) win;
  * each workout is upserted on the source's own id, so re-sending it changes nothing;
  * the same workout arriving under two ids is linked as a duplicate;
  * every day the batch touched is handed to the reward engine, which pays only the
    difference between what the day is now worth and what it was already paid.

The device sends facts. Nothing in the payload can state or influence a credit amount.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import ValidationFailed
from app.core.timeutil import day_bounds_utc, local_date
from app.models.enums import FitnessSource, WorkoutCategory
from app.models.fitness import FitnessDaily, FitnessSyncBatch, WorkoutEvent, WorkoutTypeRule
from app.models.rewards import DailyReward
from app.models.user import User
from app.models.wallet import WalletAccount
from app.services.fitness.errors import BatchIdReused, TestDataDisabled, WorkoutNotFound
from app.services.fitness.model import DayRecord, SyncBatch, WorkoutRecord
from app.services.fitness.sources import rewardable_sources
from app.services.fitness.validation import Bounds, Rejection, validate_day, validate_workout
from app.services.rewards.calculator import SECONDS_PER_MINUTE, whole_seconds
from app.services.rewards.engine import DayReward, RewardEngine
from app.services.wallet import WalletService

UNCLASSIFIED = "unclassified"
RUNNING_TYPES = frozenset({"running"})

# Two records are the same workout if they are the same type, start within two minutes
# of each other and their active time differs by no more than ten percent.
DUPLICATE_START_TOLERANCE = timedelta(minutes=2)
DUPLICATE_DURATION_TOLERANCE_PERCENT = 10

_HUNDREDTHS = Decimal("0.01")
_ONE_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class IngestOutcome:
    """What a synchronisation did. ``payload`` is JSON-safe and is what is stored for
    replays; the wallet balances are read fresh every time."""

    payload: dict[str, Any]
    replayed: bool
    available_wc: int
    reserved_wc: int


def _duration_minutes(active_seconds: int) -> Decimal:
    return (Decimal(active_seconds) / Decimal(SECONDS_PER_MINUTE)).quantize(
        _HUNDREDTHS, rounding=ROUND_FLOOR
    )


def _workout_dates(start: datetime, end: datetime, timezone: str) -> set[date]:
    first = local_date(start, timezone)
    last = local_date(end - _ONE_MICROSECOND, timezone)
    return {first + timedelta(days=offset) for offset in range(max((last - first).days, 0) + 1)}


def reward_payload(reward: DayReward) -> dict[str, Any]:
    return {
        "date": reward.activity_date.isoformat(),
        "source": reward.source,
        "is_test_data": reward.is_test_data,
        "steps": reward.steps,
        "eligible_minutes": reward.eligible_minutes,
        "step_credits_wc": reward.step_credits,
        "workout_credits_wc": reward.workout_credits,
        "entitled_wc": reward.entitled,
        "capped": reward.capped,
        "awarded_now_wc": reward.paid_now,
        "reversed_now_wc": reward.reversed_now,
        "awarded_total_wc": reward.awarded_total,
        "in_reward_window": reward.in_reward_window,
    }


class FitnessIngestionService:
    def __init__(self, session: AsyncSession, *, settings: Settings, clock: Clock) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._wallets = WalletService(session, clock)
        self._engine = RewardEngine(session, settings=settings, clock=clock)
        self._bounds = Bounds.from_settings(settings)

    # ------------------------------------------------------------------- sync

    async def ingest(
        self, user: User, batch: SyncBatch, *, device_token_id: uuid.UUID | None
    ) -> IngestOutcome:
        if batch.source is FitnessSource.TEST and not self._settings.allow_test_fitness_data:
            raise TestDataDisabled("Test fitness data is disabled (ALLOW_TEST_FITNESS_DATA).")
        if batch.source not in rewardable_sources(self._settings):
            raise ValidationFailed(f"Source {batch.source.value!r} cannot be synchronised.")
        if len(batch.days) > self._settings.max_days_per_sync:
            raise ValidationFailed(
                f"A batch may contain at most {self._settings.max_days_per_sync} days."
            )
        if len(batch.workouts) > self._settings.max_workouts_per_sync:
            raise ValidationFailed(
                f"A batch may contain at most {self._settings.max_workouts_per_sync} workouts."
            )

        # From here on this user's syncs, rewards and spending are serialised.
        wallet = await self._wallets.lock(user.id)
        fingerprint = batch.fingerprint()
        previous = await self._session.scalar(
            select(FitnessSyncBatch).where(
                FitnessSyncBatch.user_id == user.id,
                FitnessSyncBatch.client_batch_id == batch.batch_id,
            )
        )
        if previous is not None:
            if previous.payload_sha256 != fingerprint:
                raise BatchIdReused(
                    "This batch_id was already used with different content. "
                    "Use a new batch_id for new data."
                )
            return self._outcome(previous.result, wallet, replayed=True)

        now = self._clock.now()
        today = local_date(now, user.timezone)
        is_test = batch.source is FitnessSource.TEST
        record = FitnessSyncBatch(
            user_id=user.id,
            device_token_id=device_token_id,
            client_batch_id=batch.batch_id,
            source=batch.source.value,
            is_test_data=is_test,
            payload_sha256=fingerprint,
            days_received=len(batch.days),
            workouts_received=len(batch.workouts),
            received_at=now,
        )
        self._session.add(record)
        await self._session.flush()

        affected: set[date] = set()
        day_results = await self._store_days(user, batch, record.id, today, now, affected)
        categories = await self._categories()
        workout_results, touched = await self._store_workouts(
            user, batch, record.id, now, categories, affected
        )
        deleted = await self._apply_deletions(user, batch, now, affected)
        await self._session.flush()
        duplicates = await self._link_duplicates(user, touched)
        for result in workout_results:
            if result["external_id"] in duplicates:
                result["duplicate"] = True
                result["earns_minutes"] = False

        rewards = await self._reward(user, affected, today)

        payload: dict[str, Any] = {
            "batch_id": str(batch.batch_id),
            "source": batch.source.value,
            "is_test_data": is_test,
            "days": day_results,
            "workouts": workout_results,
            "deleted_workouts": deleted,
            "rewards": [reward_payload(r) for r in rewards],
        }
        record.result = payload
        await self._session.flush()
        return self._outcome(payload, wallet, replayed=False)

    async def _store_days(
        self,
        user: User,
        batch: SyncBatch,
        batch_row_id: uuid.UUID,
        today: date,
        now: datetime,
        affected: set[date],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[date] = set()
        for day in batch.days:
            rejection = validate_day(day, today=today, bounds=self._bounds)
            if rejection is None and day.activity_date in seen:
                rejection = Rejection(
                    kind="day",
                    reference=day.activity_date.isoformat(),
                    code="duplicate_in_batch",
                    detail="This date appears more than once in the batch.",
                )
            if rejection is not None:
                results.append(self._rejected(rejection, "date"))
                continue
            seen.add(day.activity_date)
            await self._upsert_day(user, batch, day, batch_row_id, now)
            affected.add(day.activity_date)
            results.append(
                {"date": day.activity_date.isoformat(), "status": "accepted", "steps": day.steps}
            )
        return results

    async def _upsert_day(
        self, user: User, batch: SyncBatch, day: DayRecord, batch_row_id: uuid.UUID, now: datetime
    ) -> None:
        row = await self._session.scalar(
            select(FitnessDaily).where(
                FitnessDaily.user_id == user.id,
                FitnessDaily.activity_date == day.activity_date,
                FitnessDaily.source == batch.source.value,
            )
        )
        if row is None:
            row = FitnessDaily(
                user_id=user.id,
                activity_date=day.activity_date,
                source=batch.source.value,
                is_test_data=batch.source is FitnessSource.TEST,
                created_at=now,
            )
            self._session.add(row)
        # Latest report wins. HealthKit's own total can legitimately change as late
        # watch data is merged in.
        row.steps = day.steps
        if day.distance_walking_running_m is not None:
            # Stored as "all walking" until running workouts are subtracted below.
            row.walking_distance_m = day.distance_walking_running_m
            row.running_distance_m = Decimal(0)
        if day.distance_cycling_m is not None:
            row.cycling_distance_m = day.distance_cycling_m
        if day.active_energy_kcal is not None:
            row.active_energy_kcal = day.active_energy_kcal
        row.device_timezone = batch.device_timezone
        row.last_batch_id = batch_row_id
        row.synced_at = now

    async def _store_workouts(
        self,
        user: User,
        batch: SyncBatch,
        batch_row_id: uuid.UUID,
        now: datetime,
        categories: dict[str, str],
        affected: set[date],
    ) -> tuple[list[dict[str, Any]], list[WorkoutEvent]]:
        results: list[dict[str, Any]] = []
        touched: list[WorkoutEvent] = []
        seen: set[str] = set()
        for workout in batch.workouts:
            rejection = validate_workout(
                workout, now=now, timezone=user.timezone, bounds=self._bounds
            )
            if rejection is None and workout.external_id in seen:
                rejection = Rejection(
                    kind="workout",
                    reference=workout.external_id,
                    code="duplicate_in_batch",
                    detail="This workout id appears more than once in the batch.",
                )
            if rejection is not None:
                results.append(self._rejected(rejection, "external_id"))
                continue
            seen.add(workout.external_id)
            row = await self._upsert_workout(user, batch, workout, batch_row_id, now, affected)
            touched.append(row)
            category = categories.get(workout.workout_type, UNCLASSIFIED)
            results.append(
                {
                    "external_id": workout.external_id,
                    "status": "accepted",
                    "type": workout.workout_type,
                    "category": category,
                    "verified": row.verified,
                    "duplicate": False,
                    "earns_minutes": (
                        row.verified
                        and not row.excluded_from_rewards
                        and category == WorkoutCategory.NON_STEP.value
                    ),
                }
            )
        return results, touched

    async def _upsert_workout(
        self,
        user: User,
        batch: SyncBatch,
        workout: WorkoutRecord,
        batch_row_id: uuid.UUID,
        now: datetime,
        affected: set[date],
    ) -> WorkoutEvent:
        row = await self._session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.user_id == user.id,
                WorkoutEvent.source == batch.source.value,
                WorkoutEvent.external_id == workout.external_id,
            )
        )
        if row is None:
            row = WorkoutEvent(
                user_id=user.id,
                source=batch.source.value,
                external_id=workout.external_id,
                is_test_data=batch.source is FitnessSource.TEST,
                created_at=now,
            )
            self._session.add(row)
        else:
            # If its time moved, the days it used to touch must be re-evaluated too.
            affected |= _workout_dates(row.start_time, row.end_time, user.timezone)

        span_seconds = whole_seconds(workout.end - workout.start)
        row.workout_type = workout.workout_type
        row.start_time = workout.start
        row.end_time = workout.end
        row.duration_minutes = _duration_minutes(min(workout.active_seconds, span_seconds))
        row.distance_m = workout.distance_m
        row.active_energy_kcal = workout.active_energy_kcal
        row.origin_bundle_id = workout.origin_bundle_id
        row.origin_device = workout.origin_device
        row.was_user_entered = workout.was_user_entered
        # Passed validation and was recorded by a device, not typed in by hand.
        row.verified = not workout.was_user_entered
        row.deleted_at = None
        row.last_batch_id = batch_row_id
        row.updated_at = now
        affected |= _workout_dates(workout.start, workout.end, user.timezone)
        return row

    async def _apply_deletions(
        self, user: User, batch: SyncBatch, now: datetime, affected: set[date]
    ) -> int:
        if not batch.deleted_workout_ids:
            return 0
        rows = (
            await self._session.scalars(
                select(WorkoutEvent).where(
                    WorkoutEvent.user_id == user.id,
                    WorkoutEvent.source == batch.source.value,
                    WorkoutEvent.external_id.in_(batch.deleted_workout_ids),
                    WorkoutEvent.deleted_at.is_(None),
                )
            )
        ).all()
        for row in rows:
            row.deleted_at = now
            row.updated_at = now
            affected |= _workout_dates(row.start_time, row.end_time, user.timezone)
        if rows:
            # A record that was only a duplicate of a now-deleted workout stands on its
            # own again; otherwise that session would silently stop counting.
            await self._session.execute(
                update(WorkoutEvent)
                .where(
                    WorkoutEvent.user_id == user.id,
                    WorkoutEvent.duplicate_of.in_([row.id for row in rows]),
                )
                .values(duplicate_of=None, updated_at=now)
            )
        return len(rows)

    async def _link_duplicates(self, user: User, touched: Sequence[WorkoutEvent]) -> set[str]:
        """Mark records that describe a workout already known under another id.

        Within a group of matching records exactly one is kept as the original: the
        most complete (longest active time), then the earliest received. The choice
        depends only on the records themselves, not on the order they arrive in, so it
        is stable from one sync to the next.

        Returns the external ids, among ``touched``, that are duplicates. The reward
        calculation would not count their minutes twice in any case (it is bounded by
        the union of intervals); this makes the reason visible to the user.
        """
        for row in touched:
            similar = [
                candidate
                for candidate in (
                    await self._session.scalars(
                        select(WorkoutEvent).where(
                            WorkoutEvent.user_id == user.id,
                            WorkoutEvent.id != row.id,
                            WorkoutEvent.workout_type == row.workout_type,
                            WorkoutEvent.deleted_at.is_(None),
                            WorkoutEvent.start_time >= row.start_time - DUPLICATE_START_TOLERANCE,
                            WorkoutEvent.start_time <= row.start_time + DUPLICATE_START_TOLERANCE,
                        )
                    )
                ).all()
                if _similar_duration(candidate, row)
            ]
            group = [row, *similar]
            original = min(group, key=_completeness_order)
            for member in group:
                member.duplicate_of = None if member is original else original.id
            await self._session.flush()
        return {row.external_id for row in touched if row.duplicate_of is not None}

    async def _reward(self, user: User, affected: set[date], today: date) -> list[DayReward]:
        rewards: list[DayReward] = []
        for day in sorted(affected):
            if day > today or (today - day).days > self._settings.sync_window_days:
                continue
            await self._refresh_distances(user, day)
            reward = await self._engine.evaluate(user, day)
            rewards.append(reward)
            if reward.source is not None:
                await self._session.execute(
                    update(FitnessDaily)
                    .where(
                        FitnessDaily.user_id == user.id,
                        FitnessDaily.activity_date == day,
                        FitnessDaily.source == reward.source,
                    )
                    .values(active_minutes=reward.eligible_minutes)
                )
        return rewards

    async def _refresh_distances(self, user: User, day: date) -> None:
        """Split the combined walking + running distance using running workouts.

        HealthKit reports one walking-and-running total. Running distance is the sum of
        that day's running workouts; walking distance is the remainder.
        """
        day_start, day_end = day_bounds_utc(day, user.timezone)
        rows = (
            await self._session.scalars(
                select(FitnessDaily).where(
                    FitnessDaily.user_id == user.id, FitnessDaily.activity_date == day
                )
            )
        ).all()
        for row in rows:
            if row.walking_distance_m is None:
                continue
            combined = row.walking_distance_m + (row.running_distance_m or Decimal(0))
            ran = await self._session.scalar(
                select(func.coalesce(func.sum(WorkoutEvent.distance_m), 0)).where(
                    WorkoutEvent.user_id == user.id,
                    WorkoutEvent.source == row.source,
                    WorkoutEvent.workout_type.in_(RUNNING_TYPES),
                    WorkoutEvent.start_time >= day_start,
                    WorkoutEvent.start_time < day_end,
                    WorkoutEvent.deleted_at.is_(None),
                    WorkoutEvent.duplicate_of.is_(None),
                )
            )
            running = min(Decimal(ran or 0), combined)
            row.running_distance_m = running
            row.walking_distance_m = combined - running

    # --------------------------------------------------------------- exclusion

    async def set_workout_exclusion(
        self, user: User, workout_id: uuid.UUID, *, excluded: bool, reason: str | None
    ) -> tuple[WorkoutEvent, list[DayReward]]:
        """Exclude a workout from rewards, or include it again.

        Excluding a workout that was already rewarded reverses those credits, as far as
        the available balance allows. This is the only path that takes credits back.
        """
        await self._wallets.lock(user.id)
        row = await self._session.scalar(
            select(WorkoutEvent)
            .where(WorkoutEvent.id == workout_id, WorkoutEvent.user_id == user.id)
            .with_for_update()
        )
        if row is None:
            raise WorkoutNotFound()
        if row.excluded_from_rewards == excluded:
            return row, []
        row.excluded_from_rewards = excluded
        row.exclusion_reason = reason if excluded else None
        row.updated_at = self._clock.now()
        await self._session.flush()

        rewards = [
            await self._engine.evaluate(user, day, allow_reversal=excluded)
            for day in sorted(_workout_dates(row.start_time, row.end_time, user.timezone))
        ]
        for reward in rewards:
            if reward.source is not None:
                await self._session.execute(
                    update(FitnessDaily)
                    .where(
                        FitnessDaily.user_id == user.id,
                        FitnessDaily.activity_date == reward.activity_date,
                        FitnessDaily.source == reward.source,
                    )
                    .values(active_minutes=reward.eligible_minutes)
                )
        return row, rewards

    async def recalculate(self, user: User, first: date, last: date) -> list[DayReward]:
        """Re-evaluate a range of days (for example after a workout type is classified)."""
        await self._wallets.lock(user.id)
        today = local_date(self._clock.now(), user.timezone)
        days = {first + timedelta(days=offset) for offset in range((last - first).days + 1)}
        return await self._reward(user, days, today)

    # ----------------------------------------------------------------- deletion

    async def delete_all_data(self, user: User) -> dict[str, int]:
        """Erase the user's stored activity.

        Days, workouts and sync records are deleted. What remains is, per day, the date
        and the credits already awarded — without that, re-synchronising the same days
        would pay them a second time. Credits already earned stay in the wallet.
        """
        await self._wallets.lock(user.id)
        counts: dict[str, int] = {}
        for name, model in (
            ("workouts", WorkoutEvent),
            ("days", FitnessDaily),
            ("sync_batches", FitnessSyncBatch),
        ):
            result = await self._session.execute(
                delete(model).where(model.user_id == user.id).returning(model.id)
            )
            counts[name] = len(result.all())
        await self._session.execute(
            update(DailyReward)
            .where(DailyReward.user_id == user.id)
            .values(
                steps_counted=0,
                minutes_counted=0,
                step_credits=0,
                workout_credits=0,
                entitled_credits=0,
                rules_snapshot={},
            )
        )
        return counts

    # ------------------------------------------------------------------ helpers

    async def _categories(self) -> dict[str, str]:
        rows = await self._session.execute(
            select(WorkoutTypeRule.workout_type, WorkoutTypeRule.category)
        )
        return {workout_type: category for workout_type, category in rows.all()}

    @staticmethod
    def _rejected(rejection: Rejection, key: str) -> dict[str, Any]:
        return {
            key: rejection.reference,
            "status": "rejected",
            "code": rejection.code,
            "detail": rejection.detail,
        }

    @staticmethod
    def _outcome(
        payload: dict[str, Any], wallet: WalletAccount, *, replayed: bool
    ) -> IngestOutcome:
        return IngestOutcome(
            payload=payload,
            replayed=replayed,
            available_wc=wallet.available_balance,
            reserved_wc=wallet.reserved_balance,
        )


def _completeness_order(workout: WorkoutEvent) -> tuple[Decimal, datetime, str]:
    """Sort key that puts the record to keep first: longest, then earliest received."""
    return (-workout.duration_minutes, workout.created_at, str(workout.id))


def _similar_duration(first: WorkoutEvent, second: WorkoutEvent) -> bool:
    longer = max(first.duration_minutes, second.duration_minutes)
    difference = abs(first.duration_minutes - second.duration_minutes)
    return difference * 100 <= longer * DUPLICATE_DURATION_TOLERANCE_PERCENT
