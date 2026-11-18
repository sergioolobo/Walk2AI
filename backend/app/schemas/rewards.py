"""Reward-rule and workout-type administration schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.enums import RewardActivityType, WorkoutCategory
from app.schemas.common import RequestModel, ResponseModel
from app.schemas.wallet import DecimalString

Rate = Annotated[Decimal, Field(ge=0, le=1000, max_digits=12, decimal_places=6)]
Limit = Annotated[int, Field(ge=0, le=1_000_000)]


class RewardRuleResponse(ResponseModel):
    id: uuid.UUID
    activity_type: str
    conversion_rate: DecimalString | None = Field(
        description="Walk Credits per unit (per step, or per eligible workout minute)."
    )
    daily_limit: int | None = Field(
        description="Cap in WC: for this activity, or for the whole day on a daily_total rule."
    )
    effective_from: date
    effective_until: date | None = Field(description="Exclusive. Null means open-ended.")
    enabled: bool
    state: Literal["scheduled", "in_force", "ended"]
    created_at: datetime


class RewardRuleCreate(RequestModel):
    activity_type: RewardActivityType
    conversion_rate: Rate | None = None
    daily_limit: Limit | None = None
    effective_from: date


class RewardRuleUpdate(RequestModel):
    """Only the fields present in the request are changed."""

    conversion_rate: Rate | None = None
    daily_limit: Limit | None = None
    effective_from: date | None = None
    effective_until: date | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "RewardRuleUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class WorkoutTypeResponse(ResponseModel):
    workout_type: str
    category: str
    updated_at: datetime


class WorkoutTypeUpdate(RequestModel):
    category: WorkoutCategory


class RecalculateRequest(RequestModel):
    user_id: uuid.UUID
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _ordered(self) -> "RecalculateRequest":
        if self.date_to < self.date_from:
            raise ValueError("date_to must not be before date_from")
        if (self.date_to - self.date_from).days > 62:
            raise ValueError("at most 62 days can be recalculated at once")
        return self
