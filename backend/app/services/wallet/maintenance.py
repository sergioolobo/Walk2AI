"""Background upkeep for the wallet: releasing holds whose request never finished.

A reservation outlives its request only if the process died mid-request. Without this
sweep those credits would stay locked forever. Each hold is expired in its own short
transaction so one failure cannot block the rest.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.services.wallet.service import WalletService

logger = logging.getLogger("walk2ai.wallet")


async def expire_stale_reservations(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, limit: int = 200
) -> int:
    """Release every expired active hold. Returns how many were released."""
    async with session_factory() as session:
        stale = await WalletService(session, clock).stale_reservation_ids(limit=limit)

    expired = 0
    for reservation_id in stale:
        try:
            async with session_factory() as session, session.begin():
                if await WalletService(session, clock).expire(reservation_id):
                    expired += 1
        except Exception:
            logger.exception(
                "failed to expire reservation", extra={"reservation_id": str(reservation_id)}
            )
    if expired:
        logger.info("expired stale reservations", extra={"count": expired})
    return expired


async def run_reservation_sweeper(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, interval: timedelta
) -> None:
    """Run ``expire_stale_reservations`` forever. Cancelled on application shutdown."""
    while True:
        await asyncio.sleep(interval.total_seconds())
        try:
            await expire_stale_reservations(session_factory, clock)
        except Exception:
            logger.exception("reservation sweep failed")
