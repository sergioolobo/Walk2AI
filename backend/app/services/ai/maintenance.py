"""Upkeep for AI requests whose process died before they were settled.

Such a request leaves a ``pending`` usage record and an active hold. The wallet sweeper
releases the hold once it expires (nothing is charged: what the provider did is not
known). This marks the usage record ``unknown`` so it stands out for reconciliation
against the provider's own usage report, instead of looking forever in progress.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.models.ai import AIUsage
from app.models.enums import ReservationStatus
from app.models.wallet import WalletReservation

logger = logging.getLogger("walk2ai.ai")

ABANDONED = "abandoned"


async def mark_abandoned_requests(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, limit: int = 200
) -> int:
    """Mark pending requests whose hold has already ended. Returns how many."""
    async with session_factory() as session, session.begin():
        rows = (
            await session.scalars(
                select(AIUsage)
                .join(WalletReservation, WalletReservation.id == AIUsage.reservation_id)
                .where(
                    AIUsage.status == "pending",
                    WalletReservation.status != ReservationStatus.ACTIVE.value,
                )
                .order_by(AIUsage.created_at)
                .limit(limit)
                .with_for_update(of=AIUsage, skip_locked=True)
            )
        ).all()
        now = clock.now()
        for usage in rows:
            usage.status = "unknown"
            usage.cost_basis = "none"
            usage.error_code = ABANDONED
            usage.error_message = (
                "The server stopped before this request was settled. Nothing was charged; "
                "check the provider's usage report."
            )
            usage.completed_at = now
    if rows:
        logger.warning("marked abandoned AI requests", extra={"count": len(rows)})
    return len(rows)
