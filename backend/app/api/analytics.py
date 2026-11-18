"""Dashboard analytics: activity, rewards, AI usage and credit flows over time.

Periods are in the user's own timezone: ``from``/``to`` are local calendar dates and
``granularity`` groups them by day, week (Monday first) or month.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ClockDep, PrincipalDep, SessionDep, SettingsDep, WalletServiceDep
from app.api.wallet import get_wallet, wallet_summary
from app.core.timeutil import local_date
from app.schemas.analytics import (
    ActivityPoint,
    ActivityReport,
    AIUsageResponse,
    DashboardResponse,
    FlowPoint,
    ModelUsagePoint,
    Period,
    RewardPoint,
    RewardReport,
    SpendingReport,
    UsageFigures,
    UsagePoint,
)
from app.services import analytics
from app.services.analytics import Granularity
from app.services.analytics.service import ActivityBucket, UsageTotals

router = APIRouter(prefix="/analytics", tags=["analytics"])

FromQuery = Annotated[date | None, Query(alias="from", description="Default: per granularity.")]
ToQuery = Annotated[date | None, Query(alias="to", description="Default: today.")]
GranularityQuery = Annotated[Granularity, Query()]


def _period(timezone: str, granularity: Granularity, first: date, last: date) -> Period:
    return Period(timezone=timezone, granularity=granularity.value, first=first, last=last)


def _activity_point(bucket: ActivityBucket) -> ActivityPoint:
    return ActivityPoint(
        first=bucket.first,
        last=bucket.last,
        steps=bucket.steps,
        eligible_minutes=bucket.eligible_minutes,
        distance_m=bucket.distance_m,
        active_energy_kcal=bucket.active_energy_kcal,
        active_days=bucket.active_days,
        test_data=bucket.test_data,
    )


def _figures(totals: UsageTotals) -> dict[str, int | Decimal | None]:
    return {
        "cost_eur_per_request": totals.cost_eur_per_request,
        "cost_eur_per_1k_tokens": totals.cost_eur_per_1k_tokens,
        "requests": totals.requests,
        "failed": totals.failed,
        "input_tokens": totals.input_tokens,
        "cached_tokens": totals.cached_tokens,
        "output_tokens": totals.output_tokens,
        "reasoning_tokens": totals.reasoning_tokens,
        "cost_eur": totals.cost_eur,
        "credits_wc": totals.credits_wc,
    }


@router.get("/activity", response_model=ActivityReport)
async def activity(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    first: FromQuery = None,
    last: ToQuery = None,
    granularity: GranularityQuery = Granularity.DAY,
) -> ActivityReport:
    """Steps, eligible workout minutes and distance per period."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = analytics.resolve_range(first, last, today, granularity)
    rows = await analytics.activity(session, settings, user, start, end, granularity)
    return ActivityReport(
        period=_period(user.timezone, granularity, start, end),
        points=[_activity_point(b) for b in rows],
        total_steps=sum(b.steps for b in rows),
        total_eligible_minutes=sum(b.eligible_minutes for b in rows),
        total_distance_m=sum((b.distance_m for b in rows), Decimal(0)),
    )


@router.get("/rewards", response_model=RewardReport)
async def rewards(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    first: FromQuery = None,
    last: ToQuery = None,
    granularity: GranularityQuery = Granularity.DAY,
) -> RewardReport:
    """Credits earned per period, split into steps and workouts, and capped days."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = analytics.resolve_range(first, last, today, granularity)
    rows = await analytics.activity(session, settings, user, start, end, granularity)
    return RewardReport(
        period=_period(user.timezone, granularity, start, end),
        points=[
            RewardPoint(
                first=b.first,
                last=b.last,
                step_wc=b.step_wc,
                workout_wc=b.workout_wc,
                earned_wc=b.earned_wc,
                capped_days=b.capped_days,
                test_data=b.test_data,
            )
            for b in rows
        ],
        total_earned_wc=sum(b.earned_wc for b in rows),
    )


@router.get("/ai-usage", response_model=AIUsageResponse)
async def ai_usage(
    principal: PrincipalDep,
    session: SessionDep,
    clock: ClockDep,
    first: FromQuery = None,
    last: ToQuery = None,
    granularity: GranularityQuery = Granularity.DAY,
) -> AIUsageResponse:
    """Requests, tokens, provider cost (EUR) and credits charged per period and per model."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = analytics.resolve_range(first, last, today, granularity)
    report = await analytics.ai_usage(session, user, start, end, granularity)
    return AIUsageResponse(
        period=_period(user.timezone, granularity, start, end),
        points=[
            UsagePoint(first=b.first, last=b.last, **_figures(b.totals)) for b in report.buckets
        ],
        by_model=[
            ModelUsagePoint(provider=m.provider, model=m.model, **_figures(m.totals))
            for m in report.by_model
        ],
        totals=UsageFigures(**_figures(report.totals)),
    )


@router.get("/spending", response_model=SpendingReport)
async def spending(
    principal: PrincipalDep,
    session: SessionDep,
    wallets: WalletServiceDep,
    clock: ClockDep,
    first: FromQuery = None,
    last: ToQuery = None,
    granularity: GranularityQuery = Granularity.DAY,
) -> SpendingReport:
    """Credits earned and spent per period, from the ledger, with today's and this
    month's totals and how much of each spending limit is used."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    start, end = analytics.resolve_range(first, last, today, granularity)
    flows = await analytics.credit_flows(session, user.id, user.timezone, start, end, granularity)
    return SpendingReport(
        period=_period(user.timezone, granularity, start, end),
        points=[
            FlowPoint(
                first=b.first,
                last=b.last,
                earned_wc=b.earned_wc,
                spent_wc=b.spent_wc,
                adjustments_wc=b.adjustments_wc,
                net_wc=b.net_wc,
            )
            for b in flows
        ],
        total_earned_wc=sum(b.earned_wc for b in flows),
        total_spent_wc=sum(b.spent_wc for b in flows),
        summary=await wallet_summary(principal, session, wallets, clock),
    )


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    wallets: WalletServiceDep,
    clock: ClockDep,
) -> DashboardResponse:
    """Everything the home screen shows, in one request."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    week = await analytics.activity(
        session, settings, user, today - timedelta(days=6), today, Granularity.DAY
    )
    usage = await analytics.ai_usage(session, user, today.replace(day=1), today, Granularity.MONTH)
    return DashboardResponse(
        wallet=await get_wallet(principal, session, wallets, clock),
        summary=await wallet_summary(principal, session, wallets, clock),
        today=_activity_point(week[-1]),
        last_7_days=[_activity_point(b) for b in week],
        ai_this_month=UsageFigures(**_figures(usage.totals)),
    )
