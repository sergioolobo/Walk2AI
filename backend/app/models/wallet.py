"""Wallet tables.

Only ``app.services.wallet.WalletService`` may write to these. The database itself
refuses negative balances, malformed ledger rows, duplicate idempotency keys and any
UPDATE / DELETE / TRUNCATE of the ledger.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, SmallInteger, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Credits, new_uuid, utcnow
from app.models.enums import ReservationStatus


class WalletAccount(Base):
    __tablename__ = "wallet_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    available_balance: Mapped[int] = mapped_column(Credits, default=0)
    reserved_balance: Mapped[int] = mapped_column(Credits, default=0)
    lifetime_earned: Mapped[int] = mapped_column(Credits, default=0)
    lifetime_spent: Mapped[int] = mapped_column(Credits, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WalletReservation(Base):
    __tablename__ = "wallet_reservations"
    __table_args__ = (
        Index(
            "ix_wallet_reservations_active",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_wallet_reservations_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallet_accounts.id", ondelete="RESTRICT")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    reserved_amount: Mapped[int] = mapped_column(Credits)
    settled_amount: Mapped[int | None] = mapped_column(Credits)
    released_amount: Mapped[int | None] = mapped_column(Credits)
    status: Mapped[str] = mapped_column(default=ReservationStatus.ACTIVE.value)
    description: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]

    @property
    def is_active(self) -> bool:
        return self.status == ReservationStatus.ACTIVE.value


class WalletTransaction(Base):
    """One immutable ledger row.

    ``amount`` is the signed change to the available balance; ``reserved_delta`` the
    signed change to the reserved balance. Balances always equal the running sums.
    """

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        Index(
            "uq_wallet_transactions_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_wallet_transactions_user_id", "user_id", "id"),
        Index("ix_wallet_transactions_user_created", "user_id", "created_at"),
        Index("ix_wallet_transactions_reference", "reference_type", "reference_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallet_accounts.id", ondelete="RESTRICT")
    )
    transaction_type: Mapped[str]
    amount: Mapped[int] = mapped_column(Credits)
    reserved_delta: Mapped[int] = mapped_column(Credits, default=0)
    balance_after: Mapped[int] = mapped_column(Credits)
    reserved_after: Mapped[int] = mapped_column(Credits)
    reference_type: Mapped[str | None]
    reference_id: Mapped[uuid.UUID | None]
    idempotency_key: Mapped[str | None]
    description: Mapped[str] = mapped_column(default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    # RESTRICT, not SET NULL: a ledger row can never be rewritten, not even by a cascade.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SpendingLimits(Base):
    """Per-user spending controls in WC. NULL means "no limit of that kind"."""

    __tablename__ = "spending_limits"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    max_request_wc: Mapped[int | None]
    max_daily_wc: Mapped[int | None]
    max_monthly_wc: Mapped[int | None]
    min_balance_wc: Mapped[int] = mapped_column(default=0)
    alert_threshold_pct: Mapped[int] = mapped_column(SmallInteger, default=80)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
