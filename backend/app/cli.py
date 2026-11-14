"""Operational commands.

python -m app.cli seed                 insert missing initial configuration
python -m app.cli reconcile            verify balances against the ledger
python -m app.cli expire-reservations  release holds past their expiry
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import build_engine, build_session_factory
from app.seeds import seed_initial_configuration
from app.services.wallet import WalletService
from app.services.wallet.maintenance import expire_stale_reservations

Command = Callable[[async_sessionmaker[AsyncSession]], Awaitable[int]]


async def _seed(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session, session.begin():
        result = await seed_initial_configuration(session)
    print(
        f"seed: {result.reward_rules_created} reward rule(s), "
        f"{result.workout_types_created} workout type(s), "
        f"{result.exchange_rates_created} conversion rate(s) created"
    )
    return 0


async def _reconcile(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        report = await WalletService(session, SystemClock()).reconcile()
    for item in report.discrepancies:
        print(
            f"MISMATCH wallet={item.wallet_id} user={item.user_id} "
            f"{item.check}: expected {item.expected}, found {item.actual}"
        )
    status = "ok" if report.ok else f"{len(report.discrepancies)} discrepancy(ies)"
    print(f"reconcile: {report.wallets_checked} wallet(s) checked, {status}")
    return 0 if report.ok else 1


async def _expire(session_factory: async_sessionmaker[AsyncSession]) -> int:
    count = await expire_stale_reservations(session_factory, SystemClock())
    print(f"expire-reservations: {count} released")
    return 0


COMMANDS: dict[str, Command] = {
    "seed": _seed,
    "reconcile": _reconcile,
    "expire-reservations": _expire,
}


async def _run(command: Command) -> int:
    engine = build_engine(get_settings())
    try:
        return await command(build_session_factory(engine))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Walk2AI operations")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    return asyncio.run(_run(COMMANDS[args.command]))


if __name__ == "__main__":
    sys.exit(main())
