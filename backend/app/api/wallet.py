"""Wallet endpoints. Read-only: no route here can change a balance.

Balances move only as a side effect of validated server-side events (activity sync, AI
usage) or an audited administrative adjustment.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ClockDep, PrincipalDep, SessionDep, WalletServiceDep
from app.core.timeutil import day_bounds_utc, local_date, month_bounds_utc
from app.models.enums import TransactionType
from app.models.wallet import WalletReservation, WalletTransaction
from app.schemas.wallet import (
    MonthTotals,
    ReservationResponse,
    TodayTotals,
    TransactionPage,
    TransactionResponse,
    WalletResponse,
    WalletSummaryResponse,
)
from app.services.wallet import queries

router = APIRouter(prefix="/wallet", tags=["wallet"])


def transaction_response(tx: WalletTransaction) -> TransactionResponse:
    return TransactionResponse(
        id=tx.id,
        type=tx.transaction_type,
        amount_wc=tx.amount,
        reserved_delta_wc=tx.reserved_delta,
        balance_after_wc=tx.balance_after,
        reserved_after_wc=tx.reserved_after,
        description=tx.description,
        reference_type=tx.reference_type,
        reference_id=tx.reference_id,
        created_at=tx.created_at,
    )


def _reservation_response(reservation: WalletReservation) -> ReservationResponse:
    return ReservationResponse(
        id=reservation.id,
        request_id=reservation.request_id,
        reserved_wc=reservation.reserved_amount,
        settled_wc=reservation.settled_amount,
        released_wc=reservation.released_amount,
        status=reservation.status,
        description=reservation.description,
        created_at=reservation.created_at,
        expires_at=reservation.expires_at,
        closed_at=reservation.closed_at,
    )


@router.get("", response_model=WalletResponse)
async def get_wallet(
    principal: PrincipalDep, session: SessionDep, wallets: WalletServiceDep, clock: ClockDep
) -> WalletResponse:
    wallet = await wallets.get_wallet(principal.user.id)
    rate = await queries.current_wc_per_eur(session, clock.now())
    return WalletResponse(
        available_wc=wallet.available_balance,
        reserved_wc=wallet.reserved_balance,
        lifetime_earned_wc=wallet.lifetime_earned,
        lifetime_spent_wc=wallet.lifetime_spent,
        wc_per_eur=rate,
        available_eur=queries.credits_to_eur(wallet.available_balance, rate) if rate else None,
        updated_at=wallet.updated_at,
    )


@router.get("/history", response_model=TransactionPage)
async def wallet_history(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    type: Annotated[TransactionType | None, Query()] = None,
) -> TransactionPage:
    """The immutable ledger, newest first."""
    rows, next_cursor = await queries.list_transactions(
        session, principal.user.id, limit=limit, cursor=cursor, transaction_type=type
    )
    return TransactionPage(items=[transaction_response(tx) for tx in rows], next_cursor=next_cursor)


@router.get("/summary", response_model=WalletSummaryResponse)
async def wallet_summary(
    principal: PrincipalDep, session: SessionDep, clock: ClockDep
) -> WalletSummaryResponse:
    """Totals for today and this month, in the user's own timezone."""
    user = principal.user
    today = local_date(clock.now(), user.timezone)
    day_start, day_end = day_bounds_utc(today, user.timezone)
    month_start, month_end = month_bounds_utc(today.year, today.month, user.timezone)
    day = await queries.period_totals(session, user.id, start=day_start, end=day_end)
    month = await queries.period_totals(session, user.id, start=month_start, end=month_end)
    return WalletSummaryResponse(
        timezone=user.timezone,
        today=TodayTotals(
            date=today, earned_wc=day.earned, spent_wc=day.spent, adjustments_wc=day.adjustments
        ),
        month=MonthTotals(
            month=f"{today.year:04d}-{today.month:02d}",
            earned_wc=month.earned,
            spent_wc=month.spent,
            adjustments_wc=month.adjustments,
        ),
    )


@router.get("/reservations", response_model=list[ReservationResponse])
async def wallet_reservations(
    principal: PrincipalDep,
    session: SessionDep,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ReservationResponse]:
    """Holds placed for AI requests: active ones and recent history."""
    rows = await queries.list_reservations(
        session, principal.user.id, limit=limit, active_only=active_only
    )
    return [_reservation_response(r) for r in rows]
