"""The background maintenance loop run by the application."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.services.ai.maintenance import mark_abandoned_requests
from app.services.wallet.maintenance import expire_stale_reservations

logger = logging.getLogger("walk2ai")


async def run_maintenance_pass(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    """Release expired holds, then flag the AI requests they belonged to. Each step is
    independent: one failing does not stop the other."""
    try:
        await expire_stale_reservations(session_factory, clock)
    except Exception:
        logger.exception("reservation sweep failed")
    try:
        await mark_abandoned_requests(session_factory, clock)
    except Exception:
        logger.exception("abandoned AI request sweep failed")


async def run_maintenance(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, interval: timedelta
) -> None:
    """Run ``run_maintenance_pass`` forever. Cancelled on application shutdown."""
    while True:
        await asyncio.sleep(interval.total_seconds())
        await run_maintenance_pass(session_factory, clock)
