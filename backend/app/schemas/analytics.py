"""Dashboard aggregations. Credits are integers; money and distances are decimal strings."""

from datetime import date

from pydantic import Field

from app.schemas.common import ResponseModel
from app.schemas.wallet import DecimalString, WalletResponse, WalletSummaryResponse


class Period(ResponseModel):
    timezone: str
    granularity: str
    first: date = Field(serialization_alias="from")
    last: date = Field(serialization_alias="to")


class ActivityPoint(ResponseModel):
    first: date = Field(serialization_alias="from")
    last: date = Field(serialization_alias="to")
    steps: int
    eligible_minutes: int = Field(description="Workout minutes that count towards credits.")
    distance_m: DecimalString = Field(description="Walking + running.")
    active_energy_kcal: DecimalString
    active_days: int
    test_data: bool = Field(description="Some of this comes from test data.")


class ActivityReport(ResponseModel):
    period: Period
    points: list[ActivityPoint]
    total_steps: int
    total_eligible_minutes: int
    total_distance_m: DecimalString


class RewardPoint(ResponseModel):
    first: date = Field(serialization_alias="from")
    last: date = Field(serialization_alias="to")
    step_wc: int
    workout_wc: int
    earned_wc: int = Field(description="What was paid, after the daily cap.")
    capped_days: int
    test_data: bool


class RewardReport(ResponseModel):
    period: Period
    points: list[RewardPoint]
    total_earned_wc: int


class UsageFigures(ResponseModel):
    requests: int
    failed: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_eur: DecimalString = Field(description="Provider cost converted to EUR.")
    credits_wc: int
    cost_eur_per_request: DecimalString | None = Field(description="Null without requests.")
    cost_eur_per_1k_tokens: DecimalString | None = Field(
        description="Per 1,000 tokens of any kind (input, cached input and output)."
    )


class UsagePoint(UsageFigures):
    first: date = Field(serialization_alias="from")
    last: date = Field(serialization_alias="to")


class ModelUsagePoint(UsageFigures):
    provider: str
    model: str


class AIUsageResponse(ResponseModel):
    period: Period
    points: list[UsagePoint]
    by_model: list[ModelUsagePoint]
    totals: UsageFigures


class FlowPoint(ResponseModel):
    first: date = Field(serialization_alias="from")
    last: date = Field(serialization_alias="to")
    earned_wc: int
    spent_wc: int
    adjustments_wc: int
    net_wc: int


class SpendingReport(ResponseModel):
    period: Period
    points: list[FlowPoint]
    total_earned_wc: int
    total_spent_wc: int
    summary: WalletSummaryResponse = Field(description="Today, this month and spending limits.")


class DashboardResponse(ResponseModel):
    wallet: WalletResponse
    summary: WalletSummaryResponse
    today: ActivityPoint
    last_7_days: list[ActivityPoint]
    ai_this_month: UsageFigures
