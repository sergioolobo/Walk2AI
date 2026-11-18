"""Wallet errors."""

from app.core.errors import AppError, Conflict, NotFound


class InsufficientCredits(AppError):
    status_code = 402
    code = "insufficient_credits"
    title = "Insufficient Walk Credits"


class InvalidAmount(AppError):
    status_code = 422
    code = "invalid_amount"
    title = "Invalid amount"


class WalletNotFound(NotFound):
    code = "wallet_not_found"
    title = "Wallet not found"


class ReservationNotFound(NotFound):
    code = "reservation_not_found"
    title = "Reservation not found"


class ReservationNotActive(Conflict):
    code = "reservation_not_active"
    title = "Reservation is no longer active"


class DuplicateRequest(Conflict):
    code = "duplicate_request"
    title = "Request id already used"


class IdempotencyConflict(Conflict):
    code = "idempotency_conflict"
    title = "Idempotency key reused with different parameters"


class LedgerInvariantError(AppError):
    """An operation would break a ledger invariant. Always a bug; never user error."""

    status_code = 500
    code = "ledger_invariant_violation"
    title = "Ledger invariant violation"
