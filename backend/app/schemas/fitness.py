"""Fitness schemas: the sync payload a device sends, and the views the web app reads.

The sync payload has no field for credits, and unknown fields are rejected: there is no
way for a device to tell the server what its activity is worth.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from app.schemas.common import RequestModel, ResponseModel
from app.schemas.wallet import DecimalString

# Hard structural bounds. Plausibility (how many steps a day can really have) is judged
# per record by the validation layer, which rejects that record and keeps the rest.
Quantity = Annotated[Decimal, Field(ge=0, le=Decimal("100000000"), max_digits=12, decimal_places=2)]
ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class DeviceInfo(RequestModel):
    timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    app_version: Annotated[str, StringConstraints(max_length=32)] | None = None


class DayIn(RequestModel):
    date: date
    steps: int = Field(ge=0, le=10_000_000)
    distance_walking_running_m: Quantity | None = None
    distance_cycling_m: Quantity | None = None
    active_energy_kcal: Quantity | None = None


class WorkoutIn(RequestModel):
    external_id: ExternalId
    type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    start: AwareDatetime
    end: AwareDatetime
    duration_seconds: int = Field(ge=0, le=7 * 24 * 3600)
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
