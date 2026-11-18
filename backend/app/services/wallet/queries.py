"""Read-only wallet queries for the API. Nothing here writes or locks."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import ExchangeRate
from app.models.enums import TransactionType
from app.models.wallet import WalletReservation, WalletTransaction

EUR = "EUR"
WALK_CREDITS = "WC"

_EARNING = (TransactionType.ACTIVITY_REWARD.value, TransactionType.REWARD_REVERSAL.value)
_ADJUSTMENTS = (TransactionType.MANUAL_ADJUSTMENT.value, TransactionType.ADMIN_ADJUSTMENT.value)
_EUR_DISPLAY_QUANTUM = Decimal("0.0001")  # 1 WC at the initial rate


@dataclass(frozen=True)
class PeriodTotals:
    earned: int
    spent: int
    adjustments: int


async def current_wc_per_eur(session: AsyncSession, at: datetime) -> Decimal | None:
    """The EUR->WC conversion rate in force at ``at`` (None if none is configured)."""
    rate: Decimal | None = await session.scalar(
        select(ExchangeRate.rate).where(
            ExchangeRate.base_currency == EUR,
            ExchangeRate.quote_currency == WALK_CREDITS,
            ExchangeRate.effective_from <= at,
            (ExchangeRate.effective_until.is_(None)) | (ExchangeRate.effective_until > at),
        )
    )
    return rate


def credits_to_eur(credits: int, wc_per_eur: Decimal) -> Decimal:
    """Display value of a credit amount. Rounded down: never overstate an allowance."""
    return (Decimal(credits) / wc_per_eur).quantize(_EUR_DISPLAY_QUANTUM, rounding=ROUND_DOWN)


async def list_transactions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    cursor: int | None = None,
    transaction_type: TransactionType | None = None,
) -> tuple[Sequence[WalletTransaction], int | None]:
    """Ledger rows, newest first. Returns (rows, cursor for the next page or None)."""
    query = select(WalletTransaction).where(WalletTransaction.user_id == user_id)
    if cursor is not None:
        query = query.where(WalletTransaction.id < cursor)
    if transaction_type is not None:
        query = query.where(WalletTransaction.transaction_type == transaction_type.value)
    rows = (
        await session.scalars(query.order_by(WalletTransaction.id.desc()).limit(limit + 1))
    ).all()
    if len(rows) > limit:
        return rows[:limit], rows[limit - 1].id
    return rows, None


async def period_totals(
    session: AsyncSession, user_id: uuid.UUID, *, start: datetime, end: datetime
) -> PeriodTotals:
    """Earned / spent / adjusted credits for ledger rows created in [start, end)."""
    tx = WalletTransaction
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(tx.amount).filter(tx.transaction_type.in_(_EARNING)), 0),
                func.coalesce(
                    func.sum(-(tx.amount + tx.reserved_delta)).filter(
                        tx.transaction_type == TransactionType.AI_USAGE.value
                    ),
                    0,
                ),
                func.coalesce(func.sum(tx.amount).filter(tx.transaction_type.in_(_ADJUSTMENTS)), 0),
            ).where(tx.user_id == user_id, tx.created_at >= start, tx.created_at < end)
        )
    ).one()
    return PeriodTotals(earned=int(row[0]), spent=int(row[1]), adjustments=int(row[2]))


async def list_reservations(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, active_only: bool
) -> Sequence[WalletReservation]:
    query = select(WalletReservation).where(WalletReservation.user_id == user_id)
    if active_only:
        query = query.where(WalletReservation.status == "active")
    result = await session.scalars(query.order_by(WalletReservation.created_at.desc()).limit(limit))
    return result.all()
