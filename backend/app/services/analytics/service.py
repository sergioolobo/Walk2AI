"""Read-only aggregations for the dashboards.

Everything is grouped by the user's own calendar: a request made at 00:30 in Lisbon
belongs to that Lisbon day, whatever the UTC date. Days are computed in the database
(``timezone(tz, created_at)``, which follows daylight saving), then rolled up into
weeks (Monday first) or months here. Every bucket in the range is returned, empty ones
included, so a chart never has gaps.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationFailed
from app.core.timeutil import day_bounds_utc
from app.models.ai import AIUsage
from app.models.enums import TransactionType
from app.models.user import User
from app.models.wallet import WalletTransaction
from app.services.fitness import queries as fitness_queries

MAX_SPAN_DAYS = 731  # two years
_AVERAGE_QUANTUM = Decimal("0.00000001")  # averages are shown to 8 decimal places


class Granularity(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


DEFAULT_SPAN = {Granularity.DAY: 30, Granularity.WEEK: 12 * 7, Granularity.MONTH: 365}


def bucket_start(day: date, granularity: Granularity) -> date:
    if granularity is Granularity.WEEK:
        return day - timedelta(days=day.weekday())
    if granularity is Granularity.MONTH:
        return day.replace(day=1)
    return day


def _next_bucket(start: date, granularity: Granularity) -> date:
    if granularity is Granularity.DAY:
        return start + timedelta(days=1)
    if granularity is Granularity.WEEK:
        return start + timedelta(days=7)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def buckets(first: date, last: date, granularity: Granularity) -> list[tuple[date, date]]:
    """Every bucket touching [first, last], as (first day, last day) — clipped to the range."""
    result = []
    start = bucket_start(first, granularity)
    while start <= last:
        following = _next_bucket(start, granularity)
        result.append((max(start, first), min(following - timedelta(days=1), last)))
        start = following
    return result


def resolve_range(
    first: date | None, last: date | None, today: date, granularity: Granularity
) -> tuple[date, date]:
    end = last or today
    start = first or end - timedelta(days=DEFAULT_SPAN[granularity] - 1)
    if start > end:
        raise ValidationFailed("'from' must not be after 'to'.")
    if (end - start).days >= MAX_SPAN_DAYS:
        raise ValidationFailed(f"The range may cover at most {MAX_SPAN_DAYS} days.")
    return start, end


def _local_day(column: Any, timezone: str) -> Any:
    """The calendar date of a timestamptz column in ``timezone`` (DST-aware)."""
    return cast(func.timezone(timezone, column), Date)


# --------------------------------------------------------------------- activity


@dataclass
class ActivityBucket:
    first: date
    last: date
    steps: int = 0
    eligible_minutes: int = 0
    distance_m: Decimal = Decimal(0)
    active_energy_kcal: Decimal = Decimal(0)
    active_days: int = 0
    step_wc: int = 0
    workout_wc: int = 0
    earned_wc: int = 0
    capped_days: int = 0
    test_data: bool = False


async def activity(
    session: AsyncSession,
    settings: Settings,
    user: User,
    first: date,
    last: date,
    granularity: Granularity,
) -> list[ActivityBucket]:
    """Steps, minutes, distance and credits earned. Distance is walking + running (the
    distances that come from the watch's step tracking); cycling is not included."""
    result = [ActivityBucket(a, b) for a, b in buckets(first, last, granularity)]
    index = {bucket.first: bucket for bucket in result}
    for view in await fitness_queries.days(session, settings, user, first, last):
        bucket = index[max(bucket_start(view.activity_date, granularity), first)]
        bucket.steps += view.steps
        bucket.eligible_minutes += view.eligible_minutes
        bucket.distance_m += (view.walking_distance_m or 0) + (view.running_distance_m or 0)
        bucket.active_energy_kcal += view.active_energy_kcal or 0
        bucket.active_days += 1 if view.steps or view.eligible_minutes else 0
        bucket.step_wc += view.step_credits
        bucket.workout_wc += view.workout_credits
        bucket.earned_wc += view.awarded
        bucket.capped_days += 1 if view.capped else 0
        bucket.test_data = bucket.test_data or view.is_test_data
    return result


# ---------------------------------------------------------------------- AI usage


@dataclass
class UsageTotals:
    requests: int = 0
    failed: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_eur: Decimal = Decimal(0)
    credits_wc: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.cached_tokens + self.output_tokens

    @property
    def cost_eur_per_request(self) -> Decimal | None:
        if not self.requests:
            return None
        return (self.cost_eur / Decimal(self.requests)).quantize(_AVERAGE_QUANTUM)

    @property
    def cost_eur_per_1k_tokens(self) -> Decimal | None:
        if not self.tokens:
            return None
        return (self.cost_eur * Decimal(1000) / Decimal(self.tokens)).quantize(_AVERAGE_QUANTUM)

    def add(self, other: "UsageTotals") -> None:
        self.requests += other.requests
        self.failed += other.failed
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_eur += other.cost_eur
        self.credits_wc += other.credits_wc


@dataclass
class UsageBucket:
    first: date
    last: date
    totals: UsageTotals = field(default_factory=UsageTotals)


@dataclass(frozen=True)
class ModelUsage:
    provider: str
    model: str
    totals: UsageTotals


@dataclass(frozen=True)
class AIUsageReport:
    buckets: list[UsageBucket]
    by_model: list[ModelUsage]
    totals: UsageTotals


def _usage_columns() -> tuple[Any, ...]:
    u = AIUsage
    return (
        func.count(),
        func.count().filter(u.status != "succeeded"),
        func.coalesce(func.sum(u.input_tokens), 0),
        func.coalesce(func.sum(u.cached_tokens), 0),
        func.coalesce(func.sum(u.output_tokens), 0),
        func.coalesce(func.sum(u.reasoning_tokens), 0),
        func.coalesce(func.sum(u.cost_eur), 0),
        func.coalesce(func.sum(u.credits_charged), 0),
    )


def _totals(values: Sequence[Any]) -> UsageTotals:
    r, f, i, c, o, x, eur, wc = values
    return UsageTotals(
        requests=int(r),
        failed=int(f),
        input_tokens=int(i),
        cached_tokens=int(c),
        output_tokens=int(o),
        reasoning_tokens=int(x),
        cost_eur=Decimal(eur),
        credits_wc=int(wc),
    )


async def ai_usage(
    session: AsyncSession, user: User, first: date, last: date, granularity: Granularity
) -> AIUsageReport:
    """Requests, tokens, provider cost (EUR) and credits charged. Requests still in
    progress are not counted; failed ones are (some were charged)."""
    start, _ = day_bounds_utc(first, user.timezone)
    _, end = day_bounds_utc(last, user.timezone)
    scope = (
        AIUsage.user_id == user.id,
        AIUsage.created_at >= start,
        AIUsage.created_at < end,
        AIUsage.status != "pending",
    )
    local_day = _local_day(AIUsage.created_at, user.timezone)

    result = [UsageBucket(a, b) for a, b in buckets(first, last, granularity)]
    index = {bucket.first: bucket for bucket in result}
    rows = await session.execute(
        select(local_day, *_usage_columns()).where(*scope).group_by(local_day)
    )
    total = UsageTotals()
    for row in rows.all():
        day, totals = row[0], _totals(row[1:])
        index[max(bucket_start(day, granularity), first)].totals.add(totals)
        total.add(totals)

    per_model = await session.execute(
        select(AIUsage.provider, AIUsage.model, *_usage_columns())
        .where(*scope)
        .group_by(AIUsage.provider, AIUsage.model)
        .order_by(func.sum(AIUsage.credits_charged).desc(), AIUsage.provider, AIUsage.model)
    )
    by_model = [
        ModelUsage(provider=row[0], model=row[1], totals=_totals(row[2:]))
        for row in per_model.all()
    ]
    return AIUsageReport(buckets=result, by_model=by_model, totals=total)


# ----------------------------------------------------------------- credit flows


@dataclass
class FlowBucket:
    first: date
    last: date
    earned_wc: int = 0
    spent_wc: int = 0
    adjustments_wc: int = 0

    @property
    def net_wc(self) -> int:
        return self.earned_wc + self.adjustments_wc - self.spent_wc


_EARNING = (TransactionType.ACTIVITY_REWARD.value, TransactionType.REWARD_REVERSAL.value)
_SPENDING = (TransactionType.AI_USAGE.value,)
_ADJUSTMENTS = (TransactionType.MANUAL_ADJUSTMENT.value, TransactionType.ADMIN_ADJUSTMENT.value)


async def credit_flows(
    session: AsyncSession,
    user_id: uuid.UUID,
    timezone: str,
    first: date,
    last: date,
    granularity: Granularity,
) -> list[FlowBucket]:
    """Credits earned (net of reversals), spent on AI, and adjusted, from the ledger.
    Holds are not spending: only settled charges count."""
    start, _ = day_bounds_utc(first, timezone)
    _, end = day_bounds_utc(last, timezone)
    tx = WalletTransaction
    local_day = _local_day(tx.created_at, timezone)
    rows = await session.execute(
        select(
            local_day,
            func.coalesce(func.sum(tx.amount).filter(tx.transaction_type.in_(_EARNING)), 0),
            func.coalesce(
                func.sum(-(tx.amount + tx.reserved_delta)).filter(
                    tx.transaction_type.in_(_SPENDING)
                ),
                0,
            ),
            func.coalesce(func.sum(tx.amount).filter(tx.transaction_type.in_(_ADJUSTMENTS)), 0),
        )
        .where(tx.user_id == user_id, tx.created_at >= start, tx.created_at < end)
        .group_by(local_day)
    )
    result = [FlowBucket(a, b) for a, b in buckets(first, last, granularity)]
    index = {bucket.first: bucket for bucket in result}
    for day, earned, spent, adjusted in rows.all():
        bucket = index[max(bucket_start(day, granularity), first)]
        bucket.earned_wc += int(earned)
        bucket.spent_wc += int(spent)
        bucket.adjustments_wc += int(adjusted)
    return result
