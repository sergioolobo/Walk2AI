"""Fitness schemas: the sync payload a device sends, and the views the web app reads.

The sync payload has no field for credits, and unknown fields are rejected: there is no
way for a device to tell the server what its activity is worth.
"""

import math
import uuid
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BeforeValidator, Field, StringConstraints

from app.schemas.common import RequestModel, ResponseModel
from app.schemas.wallet import DecimalString

_HUNDREDTH = Decimal("0.01")
_MAX_QUANTITY = Decimal("100000000")


def _as_decimal(value: Any) -> Decimal:
    """A finite number from JSON (number or numeric string) as an exact Decimal.

    Floats go through their shortest decimal representation, so 6120.38291204 becomes
    Decimal("6120.38291204"), not the binary expansion of the double.
    """
    if isinstance(value, bool):
        raise ValueError("a number is required")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("the number must be finite")
        return Decimal(repr(value))
    if isinstance(value, int | Decimal):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("a number is required") from exc
    else:
        raise ValueError("a number is required")
    if not number.is_finite():
        raise ValueError("the number must be finite")
    return number


def _quantity(value: Any) -> Any:
    """Distances and energy: HealthKit reports doubles with many decimal places. They are
    display values (credits never depend on them), so they are rounded to 0.01 rather
    than rejected."""
    if value is None:
        return None
    number = _as_decimal(value)
    if number < 0:
        raise ValueError("the value cannot be negative")
    return number.quantize(_HUNDREDTH, rounding=ROUND_HALF_EVEN)


def _whole(value: Any) -> Any:
    """Counts and durations: HealthKit sums and durations are doubles (8423.0 steps,
    1920.4532 seconds). Accepted and rounded *down*, so a fraction can never round up
    into an extra credit."""
    number = _as_decimal(value)
    if number < 0:
        raise ValueError("the value cannot be negative")
    return int(number.to_integral_value(rounding=ROUND_FLOOR))


# Hard structural bounds. Plausibility (how many steps a day can really have) is judged
# per record by the validation layer, which rejects that record and keeps the rest.
Quantity = Annotated[Decimal, BeforeValidator(_quantity), Field(ge=0, le=_MAX_QUANTITY)]
WholeSteps = Annotated[int, BeforeValidator(_whole), Field(ge=0, le=10_000_000)]
WholeSeconds = Annotated[int, BeforeValidator(_whole), Field(ge=0, le=7 * 24 * 3600)]
ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class DeviceInfo(RequestModel):
    timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    app_version: Annotated[str, StringConstraints(max_length=32)] | None = None


class DayIn(RequestModel):
    date: date
    steps: WholeSteps
    distance_walking_running_m: Quantity | None = None
    distance_cycling_m: Quantity | None = None
    active_energy_kcal: Quantity | None = None


class WorkoutIn(RequestModel):
    external_id: ExternalId
    type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    start: AwareDatetime
    end: AwareDatetime
    duration_seconds: WholeSeconds
    distance_m: Quantity | None = None
    active_energy_kcal: Quantity | None = None
    origin_bundle_id: ShortText | None = None
    origin_device: ShortText | None = None
    was_user_entered: bool = False


class SyncRequest(RequestModel):
    batch_id: uuid.UUID
    device: DeviceInfo | None = None
    days: list[DayIn] = Field(default_factory=list, max_length=400)
    workouts: list[WorkoutIn] = Field(default_factory=list, max_length=5_000)
    deleted_workout_ids: list[ExternalId] = Field(default_factory=list, max_length=5_000)


class DayResult(ResponseModel):
    date: str
    status: Literal["accepted", "rejected"]
    steps: int | None = None
    code: str | None = None
    detail: str | None = None


class WorkoutResult(ResponseModel):
    external_id: str
    status: Literal["accepted", "rejected"]
    type: str | None = None
    category: str | None = None
    verified: bool | None = None
    duplicate: bool | None = None
    earns_minutes: bool | None = None
    code: str | None = None
    detail: str | None = None


class RewardResult(ResponseModel):
    date: str
    source: str | None
    is_test_data: bool
    steps: int
    eligible_minutes: int
    step_credits_wc: int
    workout_credits_wc: int
    entitled_wc: int = Field(description="What the day is worth under its rules.")
    capped: bool
    awarded_now_wc: int = Field(description="Credits paid by this request.")
    reversed_now_wc: int
    awarded_total_wc: int = Field(description="Credits the day has been paid in total.")
    in_reward_window: bool


class WalletBalance(ResponseModel):
    available_wc: int
    reserved_wc: int


class SyncResponse(ResponseModel):
    batch_id: uuid.UUID
    replayed: bool = Field(description="True if this batch had already been processed.")
    source: str
    is_test_data: bool
    days: list[DayResult]
    workouts: list[WorkoutResult]
    deleted_workouts: int
    rewards: list[RewardResult]
    wallet: WalletBalance


class DayResponse(ResponseModel):
    date: date
    source: str | None
    is_test_data: bool
    steps: int
    walking_distance_m: DecimalString | None
    running_distance_m: DecimalString | None
    cycling_distance_m: DecimalString | None
    active_energy_kcal: DecimalString | None
    eligible_minutes: int
    step_credits_wc: int
    workout_credits_wc: int
    entitled_wc: int
    awarded_wc: int
    capped: bool
    synced_at: datetime | None


class WorkoutResponse(ResponseModel):
    id: uuid.UUID
    source: str
    external_id: str
    type: str
    category: str
    start: datetime
    end: datetime
    duration_minutes: DecimalString
    distance_m: DecimalString | None
    active_energy_kcal: DecimalString | None
    origin_bundle_id: str | None
    origin_device: str | None
    verified: bool
    is_test_data: bool
    excluded_from_rewards: bool
    exclusion_reason: str | None
    duplicate_of: uuid.UUID | None
    deleted: bool
    earns_minutes: bool
    reason: str = Field(description="Why this workout does or does not earn minutes.")


class WorkoutExclusionRequest(RequestModel):
    excluded_from_rewards: bool
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None


class WorkoutExclusionResponse(ResponseModel):
    workout: WorkoutResponse
    rewards: list[RewardResult]
    wallet: WalletBalance


class SyncStatusResponse(ResponseModel):
    last_sync_at: datetime | None
    last_source: str | None
    batches: int
    days_stored: int
    workouts_stored: int
    timezone: str
    reward_window_days: int
    sync_window_days: int


class DataDeletionResponse(ResponseModel):
    days: int
    workouts: int
    sync_batches: int
