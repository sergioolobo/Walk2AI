"""The wallet: balances, holds and the immutable ledger.

Everything that moves credits goes through ``WalletService``. No other module writes
to ``wallet_accounts``, ``wallet_transactions`` or ``wallet_reservations``.
"""

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
from app.services.wallet.limits import SpendingLimitExceeded
from app.services.wallet.service import (
    Discrepancy,
    ReconciliationReport,
    Settlement,
    WalletService,
)

__all__ = [
    "Discrepancy",
    "DuplicateRequest",
    "IdempotencyConflict",
    "InsufficientCredits",
    "InvalidAmount",
    "LedgerInvariantError",
    "ReconciliationReport",
    "ReservationNotActive",
    "ReservationNotFound",
    "Settlement",
    "SpendingLimitExceeded",
    "WalletNotFound",
    "WalletService",
]
