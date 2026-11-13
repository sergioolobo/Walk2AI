"""Administration endpoints. Every route requires the admin role; every write is audited."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import (
    AdminDep,
    ClockDep,
    RequestMetaDep,
    SessionDep,
    WalletServiceDep,
    require_admin,
)
from app.api.wallet import transaction_response
from app.core.errors import NotFound
from app.models.audit import AuditLog
from app.models.enums import ActorType, TransactionType
from app.models.user import User
from app.schemas.wallet import (
    AdjustmentRequest,
    AuditEntryResponse,
    AuditPage,
    DiscrepancyResponse,
    ReconciliationResponse,
    TransactionResponse,
)
from app.services.audit import record_audit

# The dependency is on the router itself: a route added here cannot forget it.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post(
    "/wallet/adjustments",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def adjust_wallet(
    body: AdjustmentRequest,
    admin: AdminDep,
    session: SessionDep,
    wallets: WalletServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> TransactionResponse:
    """Credit or debit a wallet by hand. Requires a reason; cannot overdraw."""
    if await session.get(User, body.user_id) is None:
        raise NotFound("User not found.")
    transaction = await wallets.adjust(
        body.user_id,
        body.amount_wc,
        kind=TransactionType.ADMIN_ADJUSTMENT,
        reason=body.reason,
        actor_id=admin.user.id,
        idempotency_key=body.idempotency_key,
    )
    record_audit(
        session,
        clock,
        action="wallet.admin_adjustment",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="wallet_transaction",
        entity_id=str(transaction.id),
        details={
            "user_id": str(body.user_id),
            "amount_wc": body.amount_wc,
            "reason": body.reason,
            "balance_after_wc": transaction.balance_after,
        },
        meta=meta,
    )
    await session.commit()
    return transaction_response(transaction)


@router.get("/wallet/reconcile", response_model=ReconciliationResponse)
async def reconcile_wallets(wallets: WalletServiceDep) -> ReconciliationResponse:
    """Check every wallet's balances against the ledger. Read-only."""
    report = await wallets.reconcile()
    return ReconciliationResponse(
        ok=report.ok,
        wallets_checked=report.wallets_checked,
        discrepancies=[DiscrepancyResponse.model_validate(d) for d in report.discrepancies],
    )


@router.get("/audit-log", response_model=AuditPage)
async def audit_log(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
) -> AuditPage:
    query = select(AuditLog)
    if cursor is not None:
        query = query.where(AuditLog.id < cursor)
    if action is not None:
        query = query.where(AuditLog.action == action)
    rows = (await session.scalars(query.order_by(AuditLog.id.desc()).limit(limit + 1))).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return AuditPage(
        items=[AuditEntryResponse.model_validate(r) for r in rows[:limit]],
        next_cursor=next_cursor,
    )
