"""WalletService — the single writer of the wallet tables.

Guarantees
  * Every balance change is exactly one immutable ledger row, written in the same
    database transaction as the balance update.
  * Every mutating method first takes ``SELECT ... FOR UPDATE`` on the user's wallet
    row. All earning and spending for one user is therefore serialised by PostgreSQL:
    two requests can never spend the same credits.
  * Lock order is always wallet row, then reservation row. No method takes them in
    the other order, so wallet operations cannot deadlock each other.
  * Balances never go negative. The service refuses first; the database CHECK
    constraints refuse again if the service is ever wrong.
  * Amounts are Python ints. There is no floating point anywhere in this package.

Transactions: methods run inside the caller's transaction and flush, but never
commit. The caller (a router, the reward engine, the AI gateway) decides when the unit
of work is complete.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.models.enums import ReservationStatus, TransactionType
from app.models.user import User
from app.models.wallet import SpendingLimits, WalletAccount, WalletReservation, WalletTransaction
from app.services.wallet.errors import (
    DuplicateRequest,
    IdempotencyConflict,
    InsufficientCredits,
    InvalidAmount,
    LedgerInvariantError,
    ReservationNotActive,
    ReservationNotFound,
    WalletNotFound,
)
from app.services.wallet.limits import enforce_limits, has_any_limit, spending_usage

_ADJUSTMENT_TYPES = (TransactionType.MANUAL_ADJUSTMENT, TransactionType.ADMIN_ADJUSTMENT)
_EARNING_TYPES = (TransactionType.ACTIVITY_REWARD.value, TransactionType.REWARD_REVERSAL.value)


@dataclass(frozen=True)
class Settlement:
    """Outcome of settling a reservation against the actual cost of a request."""

    reservation: WalletReservation
    charged: int  # credits actually deducted
    released: int  # unused part of the hold returned to the available balance
    shortfall: int  # cost that could not be collected (actual exceeded hold + balance)
    transactions: tuple[WalletTransaction, ...]


@dataclass(frozen=True)
class Discrepancy:
    wallet_id: uuid.UUID
    user_id: uuid.UUID
    check: str
    expected: int
    actual: int


@dataclass(frozen=True)
class ReconciliationReport:
    wallets_checked: int
    discrepancies: tuple[Discrepancy, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.discrepancies


def _require_positive(amount: int, what: str) -> None:
    # bool is a subclass of int; reject it explicitly along with everything non-int.
    if type(amount) is not int:
        raise InvalidAmount(f"{what} must be an integer number of Walk Credits")
    if amount <= 0:
        raise InvalidAmount(f"{what} must be greater than zero")


class WalletService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # ------------------------------------------------------------------ reads

    async def get_wallet(self, user_id: uuid.UUID) -> WalletAccount:
        wallet = await self._session.scalar(
            select(WalletAccount).where(WalletAccount.user_id == user_id)
        )
        if wallet is None:
            raise WalletNotFound()
        return wallet

    async def get_reservation(self, request_id: uuid.UUID) -> WalletReservation:
        reservation = await self._session.scalar(
            select(WalletReservation).where(WalletReservation.request_id == request_id)
        )
        if reservation is None:
            raise ReservationNotFound()
        return reservation

    # ---------------------------------------------------------------- locking

    async def lock(self, user_id: uuid.UUID) -> WalletAccount:
        """Take the user's wallet row lock for the rest of the current transaction.

        Everything that earns or spends for a user goes through this lock, so a caller
        that needs several steps to be atomic with respect to the wallet — a fitness
        sync that validates, stores and then rewards — takes it first.
        """
        return await self._lock_wallet(user_id)

    # --------------------------------------------------------------- creation

    async def create_wallet(self, user_id: uuid.UUID) -> WalletAccount:
        """Create the (empty) wallet and default spending limits for a new user."""
        now = self._clock.now()
        wallet = WalletAccount(user_id=user_id, created_at=now, updated_at=now)
        self._session.add(wallet)
        self._session.add(SpendingLimits(user_id=user_id, updated_at=now))
        await self._session.flush()
        return wallet

    # ---------------------------------------------------------------- earning

    async def reward(
        self,
        user_id: uuid.UUID,
        amount: int,
        *,
        idempotency_key: str,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> WalletTransaction:
        """Credit earned activity. Replaying the same idempotency key pays nothing."""
        _require_positive(amount, "reward")
        wallet = await self._lock_wallet(user_id)
        replay = await self._find_replay(
            wallet, idempotency_key, TransactionType.ACTIVITY_REWARD, amount
        )
        if replay is not None:
            return replay
        return await self._post(
            wallet,
            TransactionType.ACTIVITY_REWARD,
            amount=amount,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
            metadata=metadata,
        )

    async def reverse_reward(
        self,
        user_id: uuid.UUID,
        amount: int,
        *,
        idempotency_key: str,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        description: str = "",
    ) -> WalletTransaction | None:
        """Take back credits for activity that was explicitly excluded after payment.

        Capped at the available balance (the wallet never goes negative) and at what
        was ever earned (a reversal cannot claw back adjustments). Returns None when
        nothing could be reversed. The uncollected remainder is recorded in the
        transaction metadata; it is not carried as a debt.
        """
        _require_positive(amount, "reversal")
        wallet = await self._lock_wallet(user_id)
        existing = await self._find_by_key(wallet, idempotency_key)
        if existing is not None:
            if existing.transaction_type != TransactionType.REWARD_REVERSAL.value:
                raise IdempotencyConflict()
            return existing
        collectable = min(amount, wallet.available_balance, wallet.lifetime_earned)
        if collectable == 0:
            return None
        return await self._post(
            wallet,
            TransactionType.REWARD_REVERSAL,
            amount=-collectable,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
            metadata={"requested": amount, "uncollected": amount - collectable},
        )

    # ------------------------------------------------------------ adjustments

    async def adjust(
        self,
        user_id: uuid.UUID,
        amount: int,
        *,
        kind: TransactionType,
        reason: str,
        actor_id: uuid.UUID | None,
        idempotency_key: str | None = None,
    ) -> WalletTransaction:
        """Manual or administrative correction. Signed; never drives the balance negative."""
        if kind not in _ADJUSTMENT_TYPES:
            raise ValueError(f"{kind} is not an adjustment type")
        if type(amount) is not int or amount == 0:
            raise InvalidAmount("adjustment must be a non-zero integer number of Walk Credits")
        if not reason.strip():
            raise InvalidAmount("an adjustment requires a reason")
        wallet = await self._lock_wallet(user_id)
        if idempotency_key is not None:
            replay = await self._find_replay(wallet, idempotency_key, kind, amount)
            if replay is not None:
                return replay
        return await self._post(
            wallet,
            kind,
            amount=amount,
            idempotency_key=idempotency_key,
            description=reason.strip(),
            created_by=actor_id,
        )

    # ------------------------------------------------------------- spending

    async def reserve(
        self,
        user_id: uuid.UUID,
        amount: int,
        *,
        request_id: uuid.UUID,
        ttl: timedelta,
        description: str = "",
    ) -> WalletReservation:
        """Hold credits for a request that is about to be sent to a provider."""
        _require_positive(amount, "reservation")
        if ttl <= timedelta(0):
            raise ValueError("reservation ttl must be positive")
        wallet = await self._lock_wallet(user_id)

        duplicate = await self._session.scalar(
            select(WalletReservation.id).where(WalletReservation.request_id == request_id)
        )
        if duplicate is not None:
            raise DuplicateRequest()
        if wallet.available_balance < amount:
            raise InsufficientCredits(
                f"This request needs up to {amount} WC but only "
                f"{wallet.available_balance} WC are available. Earn more credits to continue.",
                extra={"required_wc": amount, "available_wc": wallet.available_balance},
            )

        now = self._clock.now()
        await self._enforce_spending_limits(wallet, amount)
        reservation = WalletReservation(
            user_id=wallet.user_id,
            wallet_id=wallet.id,
            request_id=request_id,
            reserved_amount=amount,
            status=ReservationStatus.ACTIVE.value,
            description=description,
            created_at=now,
            expires_at=now + ttl,
        )
        self._session.add(reservation)
        await self._session.flush()
        await self._post(
            wallet,
            TransactionType.CREDIT_RESERVATION,
            amount=-amount,
            reserved_delta=amount,
            reference_type="reservation",
            reference_id=reservation.id,
            description=description,
        )
        return reservation

    async def settle(
        self,
        request_id: uuid.UUID,
        actual_amount: int,
        *,
        description: str = "",
        reference_id: uuid.UUID | None = None,
    ) -> Settlement:
        """Charge the actual cost of a finished request and release the rest of the hold.

        ``actual_amount`` may be zero (nothing was billable: the whole hold is
        released) or, exceptionally, larger than the hold. In that case the excess is
        taken from the available balance as far as it goes and the remainder is
        reported as ``shortfall`` — the balance still never goes negative.
        """
        if type(actual_amount) is not int or actual_amount < 0:
            raise InvalidAmount("settled amount must be a non-negative integer")
        wallet, reservation = await self._lock_reservation(request_id)
        if not reservation.is_active:
            raise ReservationNotActive(
                f"Reservation is {reservation.status}; it cannot be settled."
            )

        held = reservation.reserved_amount
        from_hold = min(actual_amount, held)
        overflow = actual_amount - from_hold
        from_available = min(overflow, wallet.available_balance)
        shortfall = overflow - from_available
        charged = from_hold + from_available
        released = held - from_hold
        usage_reference = reference_id or reservation.id

        transactions: list[WalletTransaction] = []
        if charged > 0:
            transactions.append(
                await self._post(
                    wallet,
                    TransactionType.AI_USAGE,
                    amount=-from_available,
                    reserved_delta=-from_hold,
                    reference_type="ai_usage" if reference_id else "reservation",
                    reference_id=usage_reference,
                    description=description,
                    metadata={"shortfall": shortfall} if shortfall else None,
                )
            )
        if released > 0:
            transactions.append(
                await self._post(
                    wallet,
                    TransactionType.CREDIT_RELEASE,
                    amount=released,
                    reserved_delta=-released,
                    reference_type="reservation",
                    reference_id=reservation.id,
                    description="unused part of the hold",
                )
            )

        reservation.status = ReservationStatus.SETTLED.value
        reservation.settled_amount = charged
        reservation.released_amount = released
        reservation.closed_at = self._clock.now()
        await self._session.flush()
        return Settlement(
            reservation=reservation,
            charged=charged,
            released=released,
            shortfall=shortfall,
            transactions=tuple(transactions),
        )

    async def release(self, request_id: uuid.UUID, *, reason: str = "") -> WalletReservation:
        """Return a whole hold (the request failed before anything was billable).

        Idempotent: releasing a reservation that is already closed changes nothing, so
        failure paths can call it unconditionally.
        """
        wallet, reservation = await self._lock_reservation(request_id)
        if reservation.is_active:
            await self._close_unused(wallet, reservation, ReservationStatus.RELEASED, reason)
        return reservation

    async def expire(self, reservation_id: uuid.UUID) -> bool:
        """Release one hold whose time has run out. Returns True if it was expired."""
        request_id = await self._session.scalar(
            select(WalletReservation.request_id).where(WalletReservation.id == reservation_id)
        )
        if request_id is None:
            return False
        wallet, reservation = await self._lock_reservation(request_id)
        if not reservation.is_active or reservation.expires_at > self._clock.now():
            return False
        await self._close_unused(
            wallet, reservation, ReservationStatus.EXPIRED, "reservation expired"
        )
        return True

    async def stale_reservation_ids(self, *, limit: int = 200) -> Sequence[uuid.UUID]:
        """Active holds past their expiry, oldest first. Takes no locks."""
        result = await self._session.scalars(
            select(WalletReservation.id)
            .where(
                WalletReservation.status == ReservationStatus.ACTIVE.value,
                WalletReservation.expires_at <= self._clock.now(),
            )
            .order_by(WalletReservation.expires_at)
            .limit(limit)
        )
        return result.all()

    # ---------------------------------------------------------- reconciliation

    async def reconcile(self, user_id: uuid.UUID | None = None) -> ReconciliationReport:
        """Verify that every balance equals what the ledger says it should be.

        Read-only. A non-empty report means a bug or tampering; nothing is "repaired"
        automatically, because any repair would itself rewrite financial history.
        """
        tx = WalletTransaction
        earned_filter = tx.transaction_type.in_(_EARNING_TYPES)
        spent_filter = tx.transaction_type == TransactionType.AI_USAGE.value
        sums = (
            select(
                tx.wallet_id.label("wallet_id"),
                func.coalesce(func.sum(tx.amount), 0).label("available"),
                func.coalesce(func.sum(tx.reserved_delta), 0).label("reserved"),
                func.coalesce(func.sum(tx.amount).filter(earned_filter), 0).label("earned"),
                func.coalesce(
                    func.sum(-(tx.amount + tx.reserved_delta)).filter(spent_filter), 0
                ).label("spent"),
            )
            .group_by(tx.wallet_id)
            .subquery()
        )
        holds = (
            select(
                WalletReservation.wallet_id.label("wallet_id"),
                func.coalesce(func.sum(WalletReservation.reserved_amount), 0).label("held"),
            )
            .where(WalletReservation.status == ReservationStatus.ACTIVE.value)
            .group_by(WalletReservation.wallet_id)
            .subquery()
        )
        query = (
            select(
                WalletAccount,
                func.coalesce(sums.c.available, 0),
                func.coalesce(sums.c.reserved, 0),
                func.coalesce(sums.c.earned, 0),
                func.coalesce(sums.c.spent, 0),
                func.coalesce(holds.c.held, 0),
            )
            .outerjoin(sums, sums.c.wallet_id == WalletAccount.id)
            .outerjoin(holds, holds.c.wallet_id == WalletAccount.id)
            .order_by(WalletAccount.created_at)
        )
        if user_id is not None:
            query = query.where(WalletAccount.user_id == user_id)

        discrepancies: list[Discrepancy] = []
        rows = (await self._session.execute(query)).all()
        for wallet, available, reserved, earned, spent, held in rows:
            checks = (
                ("available_balance = sum(amount)", int(available), wallet.available_balance),
                ("reserved_balance = sum(reserved_delta)", int(reserved), wallet.reserved_balance),
                ("reserved_balance = active holds", int(held), wallet.reserved_balance),
                ("lifetime_earned = net rewards", int(earned), wallet.lifetime_earned),
                ("lifetime_spent = AI usage", int(spent), wallet.lifetime_spent),
            )
            discrepancies.extend(
                Discrepancy(wallet.id, wallet.user_id, check, expected, actual)
                for check, expected, actual in checks
                if expected != actual
            )
        return ReconciliationReport(wallets_checked=len(rows), discrepancies=tuple(discrepancies))

    # --------------------------------------------------------------- internals

    async def _enforce_spending_limits(self, wallet: WalletAccount, amount: int) -> None:
        """Apply the user's own spending limits. Runs under the wallet lock, so two
        simultaneous requests cannot both pass a limit that only one of them fits."""
        limits = await self._session.get(SpendingLimits, wallet.user_id)
        if limits is None or not has_any_limit(limits):
            return
        timezone = await self._session.scalar(
            select(User.timezone).where(User.id == wallet.user_id)
        )
        if timezone is None:  # pragma: no cover - a wallet always has a user
            raise WalletNotFound()
        usage = await spending_usage(
            self._session, wallet.user_id, timezone=timezone, now=self._clock.now()
        )
        enforce_limits(
            limits,
            usage,
            available=wallet.available_balance,
            held=wallet.reserved_balance,
            amount=amount,
        )

    async def _lock_wallet(self, user_id: uuid.UUID) -> WalletAccount:
        wallet = await self._session.scalar(
            select(WalletAccount)
            .where(WalletAccount.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            raise WalletNotFound()
        return wallet

    async def _lock_reservation(
        self, request_id: uuid.UUID
    ) -> tuple[WalletAccount, WalletReservation]:
        # Find the owner without locking, lock the wallet, then lock the reservation.
        user_id = await self._session.scalar(
            select(WalletReservation.user_id).where(WalletReservation.request_id == request_id)
        )
        if user_id is None:
            raise ReservationNotFound()
        wallet = await self._lock_wallet(user_id)
        reservation = await self._session.scalar(
            select(WalletReservation)
            .where(WalletReservation.request_id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reservation is None:  # pragma: no cover - reservations are never deleted
            raise ReservationNotFound()
        return wallet, reservation

    async def _close_unused(
        self,
        wallet: WalletAccount,
        reservation: WalletReservation,
        status: ReservationStatus,
        reason: str,
    ) -> None:
        held = reservation.reserved_amount
        await self._post(
            wallet,
            TransactionType.CREDIT_RELEASE,
            amount=held,
            reserved_delta=-held,
            reference_type="reservation",
            reference_id=reservation.id,
            description=reason,
        )
        reservation.status = status.value
        reservation.settled_amount = 0
        reservation.released_amount = held
        reservation.closed_at = self._clock.now()
        await self._session.flush()

    async def _find_by_key(
        self, wallet: WalletAccount, idempotency_key: str
    ) -> WalletTransaction | None:
        return await self._session.scalar(
            select(WalletTransaction).where(
                WalletTransaction.user_id == wallet.user_id,
                WalletTransaction.idempotency_key == idempotency_key,
            )
        )

    async def _find_replay(
        self, wallet: WalletAccount, idempotency_key: str, kind: TransactionType, amount: int
    ) -> WalletTransaction | None:
        """The earlier transaction for this key, if it is the same operation.

        Safe without further locking: the caller already holds the wallet row lock, so
        no other transaction can be inserting a ledger row for this user right now.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        existing = await self._find_by_key(wallet, idempotency_key)
        if existing is None:
            return None
        if existing.transaction_type != kind.value or existing.amount != amount:
            raise IdempotencyConflict()
        return existing

    async def _post(
        self,
        wallet: WalletAccount,
        kind: TransactionType,
        *,
        amount: int,
        reserved_delta: int = 0,
        idempotency_key: str | None = None,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
        created_by: uuid.UUID | None = None,
    ) -> WalletTransaction:
        """Apply one ledger entry to a wallet whose row lock the caller already holds."""
        new_available = wallet.available_balance + amount
        new_reserved = wallet.reserved_balance + reserved_delta
        if new_available < 0:
            raise InsufficientCredits(
                f"Only {wallet.available_balance} WC are available.",
                extra={"available_wc": wallet.available_balance},
            )
        if new_reserved < 0:
            raise LedgerInvariantError("reserved balance would become negative")

        if kind is TransactionType.ACTIVITY_REWARD:
            wallet.lifetime_earned += amount
        elif kind is TransactionType.REWARD_REVERSAL:
            wallet.lifetime_earned += amount  # negative; capped by the caller
            if wallet.lifetime_earned < 0:
                raise LedgerInvariantError("a reversal cannot exceed lifetime earnings")
        elif kind is TransactionType.AI_USAGE:
            wallet.lifetime_spent += -(amount + reserved_delta)

        now = self._clock.now()
        wallet.available_balance = new_available
        wallet.reserved_balance = new_reserved
        wallet.updated_at = now

        transaction = WalletTransaction(
            user_id=wallet.user_id,
            wallet_id=wallet.id,
            transaction_type=kind.value,
            amount=amount,
            reserved_delta=reserved_delta,
            balance_after=new_available,
            reserved_after=new_reserved,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            description=description,
            metadata_=dict(metadata or {}),
            created_by=created_by,
            created_at=now,
        )
        self._session.add(transaction)
        await self._session.flush()
        return transaction
