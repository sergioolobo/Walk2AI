"""Spending controls: the limits a user sets on their own AI spending.

Limits are checked when credits are *reserved* — before anything is sent to a provider —
under the wallet row lock, so two simultaneous requests cannot both slip under a limit.

A "day" and a "month" are the user's own (Europe/Lisbon by default), not UTC. Credits
currently held for in-flight requests count toward both: they are assumed spent until
their request settles.

All limits are optional. With none set, the only bound is the balance itself: the wallet
can never be overdrawn.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.timeutil import day_bounds_utc, local_date, month_bounds_utc
from app.models.enums import TransactionType
from app.models.wallet import SpendingLimits, WalletTransaction

PERCENT = 100


class SpendingLimitExceeded(AppError):
    status_code = 402
    code = "spending_limit_exceeded"
    title = "A spending limit would be exceeded"


@dataclass(frozen=True)
class SpendingUsage:
    spent_today: int
    spent_this_month: int


@dataclass(frozen=True)
class LimitStatus:
    """One limit, how much of it is used, and what is left."""

    limit: int | None
    used: int  # charged in the period
    held: int  # reserved for requests still in flight
    alert: bool  # usage has reached the user's alert threshold

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used - self.held)


async def _spent_between(
    session: AsyncSession, user_id: uuid.UUID, start: datetime, end: datetime
) -> int:
    tx = WalletTransaction
    total = await session.scalar(
        select(func.coalesce(func.sum(-(tx.amount + tx.reserved_delta)), 0)).where(
            tx.user_id == user_id,
            tx.transaction_type == TransactionType.AI_USAGE.value,
            tx.created_at >= start,
            tx.created_at < end,
        )
    )
    return int(total or 0)


async def spending_usage(
    session: AsyncSession, user_id: uuid.UUID, *, timezone: str, now: datetime
) -> SpendingUsage:
    """Credits charged for AI usage so far today and this month, in the user's timezone."""
    today = local_date(now, timezone)
    day_start, day_end = day_bounds_utc(today, timezone)
    month_start, month_end = month_bounds_utc(today.year, today.month, timezone)
    return SpendingUsage(
        spent_today=await _spent_between(session, user_id, day_start, day_end),
        spent_this_month=await _spent_between(session, user_id, month_start, month_end),
    )


def has_any_limit(limits: SpendingLimits | None) -> bool:
    return limits is not None and (
        limits.max_request_wc is not None
        or limits.max_daily_wc is not None
        or limits.max_monthly_wc is not None
        or limits.min_balance_wc > 0
    )


def enforce_limits(
    limits: SpendingLimits, usage: SpendingUsage, *, available: int, held: int, amount: int
) -> None:
    """Raise SpendingLimitExceeded if reserving ``amount`` would break a limit."""

    def refuse(which: str, detail: str, **extra: int) -> SpendingLimitExceeded:
        return SpendingLimitExceeded(
            detail, extra={"limit": which, "requested_wc": amount, **extra}
        )

    if limits.max_request_wc is not None and amount > limits.max_request_wc:
        raise refuse(
            "per_request",
            f"This request could cost up to {amount} WC; your per-request limit is "
            f"{limits.max_request_wc} WC.",
            limit_wc=limits.max_request_wc,
        )
    if limits.max_daily_wc is not None:
        committed = usage.spent_today + held
        if committed + amount > limits.max_daily_wc:
            raise refuse(
                "daily",
                f"Your daily spending limit is {limits.max_daily_wc} WC and {committed} WC "
                "are already spent or held today.",
                limit_wc=limits.max_daily_wc,
                used_wc=committed,
            )
    if limits.max_monthly_wc is not None:
        committed = usage.spent_this_month + held
        if committed + amount > limits.max_monthly_wc:
            raise refuse(
                "monthly",
                f"Your monthly spending limit is {limits.max_monthly_wc} WC and {committed} WC "
                "are already spent or held this month.",
                limit_wc=limits.max_monthly_wc,
                used_wc=committed,
            )
    if available - amount < limits.min_balance_wc:
        raise refuse(
            "min_balance",
            f"This request would take your balance below the {limits.min_balance_wc} WC "
            "you have chosen to keep in reserve.",
            limit_wc=limits.min_balance_wc,
            available_wc=available,
        )


async def get_limits(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> SpendingLimits:
    """The user's limits row, created with "no limits" if it does not exist yet."""
    limits = await session.get(SpendingLimits, user_id)
    if limits is None:
        limits = SpendingLimits(user_id=user_id, updated_at=now)
        session.add(limits)
        await session.flush()
    return limits


async def replace_limits(
    session: AsyncSession,
    user_id: uuid.UUID,
    now: datetime,
    *,
    max_request_wc: int | None,
    max_daily_wc: int | None,
    max_monthly_wc: int | None,
    min_balance_wc: int,
    alert_threshold_pct: int,
) -> tuple[SpendingLimits, dict[str, dict[str, int | None]]]:
    """Set all limits. Returns the row and what changed (for the audit trail)."""
    limits = await get_limits(session, user_id, now)
    wanted: dict[str, int | None] = {
        "max_request_wc": max_request_wc,
        "max_daily_wc": max_daily_wc,
        "max_monthly_wc": max_monthly_wc,
        "min_balance_wc": min_balance_wc,
        "alert_threshold_pct": alert_threshold_pct,
    }
    changes: dict[str, dict[str, int | None]] = {}
    for name, value in wanted.items():
        current = getattr(limits, name)
        if current != value:
            changes[name] = {"from": current, "to": value}
            setattr(limits, name, value)
    if changes:
        limits.updated_at = now
        await session.flush()
    return limits, changes


def limit_status(limit: int | None, used: int, held: int, threshold_pct: int) -> LimitStatus:
    alert = limit is not None and (used + held) * PERCENT >= limit * threshold_pct
    return LimitStatus(limit=limit, used=used, held=held, alert=alert)
