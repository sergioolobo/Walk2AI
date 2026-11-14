"""Fitness endpoints: synchronisation from a device, and the views the web app reads."""

import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    AdminDep,
    ClockDep,
    FitnessDeviceDep,
    FitnessReaderDep,
    IngestionServiceDep,
    PrincipalDep,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    WalletServiceDep,
)
from app.core.errors import ValidationFailed
from app.core.ratelimit import rate_limit
from app.core.timeutil import local_date
from app.models.enums import ActorType, FitnessSource
from app.schemas.fitness import (
    DataDeletionResponse,
    DayResponse,
    RewardResult,
    SyncRequest,
    SyncResponse,
    SyncStatusResponse,
    WalletBalance,
    WorkoutExclusionRequest,
    WorkoutExclusionResponse,
    WorkoutResponse,
)
from app.services.audit import record_audit
from app.services.fitness import queries
from app.services.fitness.errors import TestDataDisabled
from app.services.fitness.ingestion import IngestOutcome, reward_payload
from app.services.fitness.model import (
    DayRecord,
    SyncBatch,
    WorkoutRecord,
    normalise_workout_type,
)

router = APIRouter(prefix="/fitness", tags=["fitness"])

DEFAULT_HISTORY_DAYS = 30


def _to_batch(body: SyncRequest, source: FitnessSource) -> SyncBatch:
    """The wire payload as normalised records. This is the HealthKit adapter: another
    source would build the same SyncBatch from its own API's responses."""
    return SyncBatch(
        batch_id=body.batch_id,
        source=source,
        device_timezone=body.device.timezone if body.device else None,
        days=tuple(
            DayRecord(
                activity_date=day.date,
                steps=day.steps,
                distance_walking_running_m=day.distance_walking_running_m,
                distance_cycling_m=day.distance_cycling_m,
                active_energy_kcal=day.active_energy_kcal,
            )
            for day in body.days
        ),
        workouts=tuple(
            WorkoutRecord(
                external_id=workout.external_id,
                workout_type=normalise_workout_type(workout.type),
                start=workout.start,
                end=workout.end,
                active_seconds=workout.duration_seconds,
                distance_m=workout.distance_m,
                active_energy_kcal=workout.active_energy_kcal,
                origin_bundle_id=workout.origin_bundle_id,
                origin_device=workout.origin_device,
                was_user_entered=workout.was_user_entered,
            )
            for workout in body.workouts
        ),
        deleted_workout_ids=tuple(body.deleted_workout_ids),
    )


def _sync_response(outcome: IngestOutcome) -> SyncResponse:
    return SyncResponse.model_validate(
        {
            **outcome.payload,
            "replayed": outcome.replayed,
            "wallet": {"available_wc": outcome.available_wc, "reserved_wc": outcome.reserved_wc},
        }
    )


def _day_response(view: queries.DayView) -> DayResponse:
    return DayResponse(
        date=view.activity_date,
        source=view.source,
        is_test_data=view.is_test_data,
        steps=view.steps,
        walking_distance_m=view.walking_distance_m,
        running_distance_m=view.running_distance_m,
        cycling_distance_m=view.cycling_distance_m,
        active_energy_kcal=view.active_energy_kcal,
        eligible_minutes=view.eligible_minutes,
        step_credits_wc=view.step_credits,
        workout_credits_wc=view.workout_credits,
        entitled_wc=view.entitled,
        awarded_wc=view.awarded,
        capped=view.capped,
        synced_at=view.synced_at,
    )


def _workout_response(view: queries.WorkoutView) -> WorkoutResponse:
    workout = view.workout
    return WorkoutResponse(
        id=workout.id,
        source=workout.source,
        external_id=workout.external_id,
        type=workout.workout_type,
        category=view.category,
        start=workout.start_time,
        end=workout.end_time,
        duration_minutes=workout.duration_minutes,
        distance_m=workout.distance_m,
        active_energy_kcal=workout.active_energy_kcal,
        origin_bundle_id=workout.origin_bundle_id,
        origin_device=workout.origin_device,
        verified=workout.verified,
        is_test_data=workout.is_test_data,
        excluded_from_rewards=workout.excluded_from_rewards,
        exclusion_reason=workout.exclusion_reason,
        duplicate_of=workout.duplicate_of,
        deleted=workout.deleted_at is not None,
        earns_minutes=view.earns_minutes,
        reason=view.reason,
    )


def _date_range(
    first: date | None, last: date | None, today: date, default_days: int
) -> tuple[date, date]:
    end = last or today
    start = first or end - timedelta(days=default_days - 1)
    if start > end:
        raise ValidationFailed("'from' must not be after 'to'.")
    if end - start > queries.MAX_RANGE:
        raise ValidationFailed("The range may cover at most 366 days.")
    return start, end


# ------------------------------------------------------------------------- sync


@router.post(
    "/sync",
    response_model=SyncResponse,
    dependencies=[Depends(rate_limit("fitness-sync", limit=120, window_seconds=60))],
)
async def sync(
    body: SyncRequest, device: FitnessDeviceDep, session: SessionDep, ingestion: IngestionServiceDep
) -> SyncResponse:
    """Upload activity from a paired device (device token with ``fitness:sync``).

    Idempotent on ``batch_id``. The payload carries facts only; the server validates
    them and decides what they earn.
    """
    outcome = await ingestion.ingest(
        device.user,
        _to_batch(body, FitnessSource.APPLE_HEALTHKIT),
        device_token_id=device.device.id,
    )
    await session.commit()
    return _sync_response(outcome)


@router.post("/sync/test", response_model=SyncResponse)
async def sync_test_data(
    body: SyncRequest,
    admin: AdminDep,
    session: SessionDep,
    ingestion: IngestionServiceDep,
    settings: SettingsDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> SyncResponse:
    """Submit clearly-labelled TEST activity (administrator only, development only).

    Exists so the reward engine can be exercised without a device. Every stored row,
    reward and ledger entry it produces is marked as test data. Refused unless
    ALLOW_TEST_FITNESS_DATA is enabled, which production configuration forbids.
    """
    if not settings.allow_test_fitness_data:
        raise TestDataDisabled("Test fitness data is disabled (ALLOW_TEST_FITNESS_DATA).")
    outcome = await ingestion.ingest(
        admin.user, _to_batch(body, FitnessSource.TEST), device_token_id=None
    )
    if not outcome.replayed:
        record_audit(
            session,
            clock,
            action="fitness.test_data_submitted",
            actor_type=ActorType.ADMIN,
            actor_user_id=admin.user.id,
            entity_type="fitness_sync_batch",
            entity_id=body.batch_id,
            details={"days": len(body.days), "workouts": len(body.workouts)},
            meta=meta,
        )
    await session.commit()
    return _sync_response(outcome)


@router.get("/sync/status", response_model=SyncStatusResponse)
async def sync_status(
    user: FitnessReaderDep, session: SessionDep, settings: SettingsDep
) -> SyncStatusResponse:
    """When activity last arrived, and the rules a device should sync by.

    Accepts a browser session or a device token. A device uses ``timezone`` to bucket
    activity into the same local days the server rewards.
    """
    status_view = await queries.sync_status(session, user.id)
    await session.commit()  # persists the device's last-seen time, if a device asked
    return SyncStatusResponse(
        last_sync_at=status_view.last_sync_at,
        last_source=status_view.last_source,
        batches=status_view.batches,
        days_stored=status_view.days_stored,
        workouts_stored=status_view.workouts_stored,
        timezone=user.timezone,
        reward_window_days=settings.reward_window_days,
        sync_window_days=settings.sync_window_days,
    )


# ------------------------------------------------------------------------ views


@router.get("/daily", response_model=DayResponse)
async def daily(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> DayResponse:
    """One day (default: today in the user's timezone): activity and credits earned."""
    user = principal.user
    target = day or local_date(clock.now(), user.timezone)
    views = await queries.days(session, settings, user, target, target)
    if views:
        return _day_response(views[0])
    return DayResponse(
        date=target,
        source=None,
        is_test_data=False,
        steps=0,
        walking_distance_m=None,
        running_distance_m=None,
        cycling_distance_m=None,
        active_energy_kcal=None,
        eligible_minutes=0,
        step_credits_wc=0,
        workout_credits_wc=0,
        entitled_wc=0,
        awarded_wc=0,
        capped=False,
        synced_at=None,
    )


@router.get("/history", response_model=list[DayResponse])
async def history(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    first: Annotated[date | None, Query(alias="from")] = None,
    last: Annotated[date | None, Query(alias="to")] = None,
) -> list[DayResponse]:
    """Days with activity or rewards in a range (default: the last 30 days), newest first."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = _date_range(first, last, today, DEFAULT_HISTORY_DAYS)
    return [_day_response(v) for v in await queries.days(session, settings, user, start, end)]


@router.get("/workouts", response_model=list[WorkoutResponse])
async def workouts(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    first: Annotated[date | None, Query(alias="from")] = None,
    last: Annotated[date | None, Query(alias="to")] = None,
    include_deleted: bool = False,
) -> list[WorkoutResponse]:
    """Workouts in a range, each with whether it earns minutes and why."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = _date_range(first, last, today, DEFAULT_HISTORY_DAYS)
    views = await queries.workouts(
        session, settings, user, start, end, include_deleted=include_deleted
    )
    return [_workout_response(v) for v in views]


@router.patch("/workouts/{workout_id}", response_model=WorkoutExclusionResponse)
async def set_workout_exclusion(
    workout_id: uuid.UUID,
    body: WorkoutExclusionRequest,
    principal: PrincipalDep,
    session: SessionDep,
    ingestion: IngestionServiceDep,
    wallets: WalletServiceDep,
    settings: SettingsDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> WorkoutExclusionResponse:
    """Exclude a workout from rewards, or include it again.

    Excluding a workout that was already rewarded takes those credits back, as far as
    the available balance allows.
    """
    user = principal.user
    workout, rewards = await ingestion.set_workout_exclusion(
        user, workout_id, excluded=body.excluded_from_rewards, reason=body.reason
    )
    if rewards:
        record_audit(
            session,
            clock,
            action="fitness.workout_excluded"
            if body.excluded_from_rewards
            else "fitness.workout_included",
            actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
            actor_user_id=user.id,
            entity_type="workout",
            entity_id=workout.id,
            details={
                "reason": body.reason,
                "reversed_wc": sum(r.reversed_now for r in rewards),
                "awarded_wc": sum(r.paid_now for r in rewards),
            },
            meta=meta,
        )
    wallet = await wallets.get_wallet(user.id)
    view = await queries.workout_view(session, settings, workout)
    await session.commit()
    return WorkoutExclusionResponse(
        workout=_workout_response(view),
        rewards=[RewardResult.model_validate(reward_payload(r)) for r in rewards],
        wallet=WalletBalance(
            available_wc=wallet.available_balance, reserved_wc=wallet.reserved_balance
        ),
    )


# --------------------------------------------------------------------- deletion


@router.delete("/data", response_model=DataDeletionResponse, status_code=status.HTTP_200_OK)
async def delete_fitness_data(
    principal: PrincipalDep,
    session: SessionDep,
    ingestion: IngestionServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> DataDeletionResponse:
    """Erase stored activity (days, workouts, sync records).

    Credits already earned stay in the wallet. Per day, only the date and the credits
    awarded are kept, so that synchronising the same days again cannot pay them twice.
    """
    user = principal.user
    counts = await ingestion.delete_all_data(user)
    record_audit(
        session,
        clock,
        action="fitness.data_deleted",
        actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        details=counts,
        meta=meta,
    )
    await session.commit()
    return DataDeletionResponse(**counts)
