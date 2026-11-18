"""Concurrency: truly simultaneous wallet operations on separate database connections.

Each task opens its own session (its own PostgreSQL connection and transaction), and
the tasks are released together, so they genuinely contend for the wallet row lock.
A whole scenario is bounded by a timeout: a deadlock fails the test instead of
hanging it.
"""

import asyncio
import random
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import ReservationStatus, TransactionType
from app.models.wallet import WalletReservation, WalletTransaction
from app.services.wallet import (
    DuplicateRequest,
    InsufficientCredits,
    ReservationNotActive,
    WalletService,
)
from app.services.wallet.maintenance import expire_stale_reservations
from tests.support import (
    MutableClock,
    assert_ledger_consistent,
    create_user_with_wallet,
    wallet_state,
)

pytestmark = [pytest.mark.anyio, pytest.mark.concurrency]

Sessions = async_sessionmaker[AsyncSession]
TTL = timedelta(minutes=5)
SCENARIO_TIMEOUT = 60


async def run_concurrently[T](
    operations: Sequence[Callable[[], Awaitable[T]]],
) -> list[T | BaseException]:
    """Start every operation at the same moment; collect results and exceptions."""
    gate = asyncio.Event()

    async def guarded(operation: Callable[[], Awaitable[T]]) -> T:
        await gate.wait()
        return await operation()

    tasks = [asyncio.ensure_future(guarded(op)) for op in operations]
    await asyncio.sleep(0)  # let every task reach the gate
    gate.set()
    async with asyncio.timeout(SCENARIO_TIMEOUT):
        return await asyncio.gather(*tasks, return_exceptions=True)


def outcomes(results: Sequence[object]) -> Counter[str]:
    return Counter(type(r).__name__ if isinstance(r, Exception) else "ok" for r in results)


async def test_concurrent_reservations_cannot_spend_the_same_credits(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """1,000 WC and twenty simultaneous requests for 300 WC: exactly three can win."""
    user = await create_user_with_wallet(session_factory, clock, balance=1000)

    async def reserve() -> uuid.UUID:
        async with session_factory() as session, session.begin():
            reservation = await WalletService(session, clock).reserve(
                user, 300, request_id=uuid.uuid4(), ttl=TTL
            )
            return reservation.id

    results = await run_concurrently([reserve] * 20)

    assert outcomes(results) == {"ok": 3, "InsufficientCredits": 17}
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (100, 900)
    await assert_ledger_consistent(session_factory, clock)


async def test_database_refuses_an_overdraft_even_if_the_application_forgets_to_lock(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Negative control: a naive read-then-write, with no row lock.

    Every transaction reads the same balance before any of them writes (the barrier
    proves the reads really overlapped), so the application-level check is useless.
    The CHECK constraint on the wallet is the last line of defence, and it holds.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    contenders = 8
    all_have_read = asyncio.Barrier(contenders)

    async def naive_spend() -> int:
        async with session_factory() as session, session.begin():
            seen = await session.scalar(
                text("SELECT available_balance FROM wallet_accounts WHERE user_id = :u"),
                {"u": user},
            )
            await all_have_read.wait()
            if seen >= 300:  # stale by the time it is acted on
                await session.execute(
                    text(
                        "UPDATE wallet_accounts SET available_balance = available_balance - 300 "
                        "WHERE user_id = :u"
                    ),
                    {"u": user},
                )
            return int(seen)

    results = await run_concurrently([naive_spend] * contenders)

    assert [r for r in results if isinstance(r, int)] == [1000, 1000, 1000]
    refused = [r for r in results if isinstance(r, IntegrityError)]
    assert len(refused) == contenders - 3
    assert all("ck_wallet_accounts_available_nonnegative" in str(r) for r in refused)
    assert (await wallet_state(session_factory, user)).available_balance == 100


async def test_concurrent_reservations_of_uneven_sizes_never_overdraw(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=2500)
    amounts = [700, 50, 1200, 300, 900, 10, 450, 800, 5, 1500, 60, 240, 1000, 330, 75, 620]

    def reserve(amount: int) -> Callable[[], Awaitable[int]]:
        async def operation() -> int:
            async with session_factory() as session, session.begin():
                await WalletService(session, clock).reserve(
                    user, amount, request_id=uuid.uuid4(), ttl=TTL
                )
                return amount

        return operation

    results = await run_concurrently([reserve(a) for a in amounts])

    granted = sum(r for r in results if isinstance(r, int))
    assert all(isinstance(r, int | InsufficientCredits) for r in results)
    assert 0 < granted <= 2500
    wallet = await wallet_state(session_factory, user)
    assert wallet.available_balance == 2500 - granted
    assert wallet.reserved_balance == granted
    await assert_ledger_consistent(session_factory, clock)


async def test_a_request_id_is_reserved_only_once_under_concurrency(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """A double-submitted request (same request id) holds credits exactly once."""
    user = await create_user_with_wallet(session_factory, clock, balance=5000)
    request_id = uuid.uuid4()

    async def reserve() -> None:
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 100, request_id=request_id, ttl=TTL)

    results = await run_concurrently([reserve] * 12)

    assert outcomes(results) == {"ok": 1, "DuplicateRequest": 11}
    assert all(isinstance(r, DuplicateRequest) for r in results if r is not None)
    assert (await wallet_state(session_factory, user)).reserved_balance == 100
    await assert_ledger_consistent(session_factory, clock)


async def test_concurrent_settlement_charges_exactly_once(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 500, request_id=request_id, ttl=TTL)

    async def settle() -> int:
        async with session_factory() as session, session.begin():
            return (await WalletService(session, clock).settle(request_id, 200)).charged

    results = await run_concurrently([settle] * 10)

    assert outcomes(results) == {"ok": 1, "ReservationNotActive": 9}
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance, wallet.lifetime_spent) == (
        800,
        0,
        200,
    )
    await assert_ledger_consistent(session_factory, clock)


async def test_settle_racing_release_resolves_to_one_consistent_outcome(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Success and failure handlers firing together: charged once, or not at all."""
    for _ in range(8):  # the winner depends on scheduling; repeat to see both orders
        user = await create_user_with_wallet(session_factory, clock, balance=1000)
        request_id = uuid.uuid4()
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 500, request_id=request_id, ttl=TTL)

        async def settle(request_id: uuid.UUID = request_id) -> str:
            async with session_factory() as session, session.begin():
                await WalletService(session, clock).settle(request_id, 300)
                return "settled"

        async def release(request_id: uuid.UUID = request_id) -> str:
            async with session_factory() as session, session.begin():
                reservation = await WalletService(session, clock).release(request_id)
                return reservation.status

        settle_result, release_result = await run_concurrently([settle, release])

        wallet = await wallet_state(session_factory, user)
        assert wallet.reserved_balance == 0
        if isinstance(settle_result, ReservationNotActive):
            # Release won: nothing was charged.
            assert release_result == ReservationStatus.RELEASED.value
            assert (wallet.available_balance, wallet.lifetime_spent) == (1000, 0)
        else:
            # Settle won: the later release saw a closed reservation and did nothing.
            assert settle_result == "settled"
            assert release_result == ReservationStatus.SETTLED.value
            assert (wallet.available_balance, wallet.lifetime_spent) == (700, 300)
    await assert_ledger_consistent(session_factory, clock)


async def test_concurrent_identical_rewards_are_paid_once(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """The same day's reward arriving from fifteen simultaneous syncs pays once."""
    user = await create_user_with_wallet(session_factory, clock)

    async def reward() -> int:
        async with session_factory() as session, session.begin():
            tx = await WalletService(session, clock).reward(
                user, 950, idempotency_key="reward:2026-11-12"
            )
            return tx.id

    results = await run_concurrently([reward] * 15)

    assert outcomes(results) == {"ok": 15}
    assert len(set(results)) == 1  # every caller was handed the same ledger row
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.lifetime_earned) == (950, 950)
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.user_id == user)
        )
    assert count == 1
    await assert_ledger_consistent(session_factory, clock)


async def test_earning_and_spending_at_the_same_time_stay_consistent(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=500)

    def reward(day: int) -> Callable[[], Awaitable[None]]:
        async def operation() -> None:
            async with session_factory() as session, session.begin():
                await WalletService(session, clock).reward(user, 100, idempotency_key=f"day-{day}")

        return operation

    async def spend() -> None:
        request_id = uuid.uuid4()
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 200, request_id=request_id, ttl=TTL)
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).settle(request_id, 150)

    results = await run_concurrently([reward(d) for d in range(10)] + [spend] * 10)

    assert all(r is None or isinstance(r, InsufficientCredits) for r in results)
    spends = sum(1 for r in results[10:] if r is None)
    wallet = await wallet_state(session_factory, user)
    assert wallet.lifetime_earned == 1000
    assert wallet.lifetime_spent == 150 * spends
    assert wallet.available_balance == 500 + 1000 - 150 * spends
    assert wallet.reserved_balance == 0
    await assert_ledger_consistent(session_factory, clock)


async def test_expiry_sweep_racing_settlement_never_double_counts(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """A late settlement and the expiry sweep meet: one of them closes the hold."""
    for _ in range(6):
        user = await create_user_with_wallet(session_factory, clock, balance=1000)
        request_id = uuid.uuid4()
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(
                user, 400, request_id=request_id, ttl=timedelta(minutes=1)
            )
        clock.advance(minutes=2)

        async def settle(request_id: uuid.UUID = request_id) -> str:
            async with session_factory() as session, session.begin():
                await WalletService(session, clock).settle(request_id, 250)
                return "settled"

        async def sweep() -> int:
            return await expire_stale_reservations(session_factory, clock)

        race: list[Callable[[], Awaitable[object]]] = [settle, sweep]
        settle_result, _ = await run_concurrently(race)

        wallet = await wallet_state(session_factory, user)
        assert wallet.reserved_balance == 0
        if isinstance(settle_result, ReservationNotActive):
            assert (wallet.available_balance, wallet.lifetime_spent) == (1000, 0)
        else:
            assert (wallet.available_balance, wallet.lifetime_spent) == (750, 250)
    await assert_ledger_consistent(session_factory, clock)


async def test_randomised_concurrent_workload_keeps_every_invariant(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """A fixed-seed storm of mixed operations across several wallets.

    Whatever interleaving the database chooses: no balance goes negative, no
    deadlock occurs, and every balance still equals the sum of its ledger.
    """
    rng = random.Random(20261113)  # noqa: S311 - reproducible test data, not security
    users = [
        await create_user_with_wallet(session_factory, clock, balance=rng.randint(0, 3000))
        for _ in range(4)
    ]
    open_requests: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {u: [] for u in users}

    async def worker(worker_id: int, seed: int) -> Counter[str]:
        local = random.Random(seed)  # noqa: S311
        seen: Counter[str] = Counter()
        for step in range(30):
            user = local.choice(users)
            action = local.choice(["reward", "reserve", "reserve", "settle", "release", "adjust"])
            reserved: tuple[uuid.UUID, int] | None = None
            try:
                async with session_factory() as session, session.begin():
                    wallets = WalletService(session, clock)
                    if action == "reward":
                        await wallets.reward(
                            user, local.randint(1, 400), idempotency_key=f"w{worker_id}-s{step}"
                        )
                    elif action == "reserve":
                        request_id, amount = uuid.uuid4(), local.randint(1, 600)
                        await wallets.reserve(user, amount, request_id=request_id, ttl=TTL)
                        reserved = (request_id, amount)
                    elif action in ("settle", "release") and open_requests[user]:
                        request_id, amount = open_requests[user].pop(
                            local.randrange(len(open_requests[user]))
                        )
                        if action == "settle":
                            # Sometimes above the hold, to exercise the overflow path.
                            await wallets.settle(request_id, local.randint(0, amount + 50))
                        else:
                            await wallets.release(request_id)
                    elif action == "adjust":
                        await wallets.adjust(
                            user,
                            local.choice([-300, -50, 75, 200]),
                            kind=TransactionType.ADMIN_ADJUSTMENT,
                            reason="randomised workload",
                            actor_id=None,
                        )
                # Only now is the reservation committed and visible to other workers.
                # Sharing it any earlier would let another worker try to settle a hold
                # that does not exist yet — as a real request never could.
                if reserved is not None:
                    open_requests[user].append(reserved)
                seen[action] += 1
            except (InsufficientCredits, ReservationNotActive) as refused:
                seen[type(refused).__name__] += 1
        return seen

    seeds = [rng.randint(0, 10**9) for _ in range(12)]

    def start(worker_id: int) -> Callable[[], Awaitable[Counter[str]]]:
        return lambda: worker(worker_id, seeds[worker_id])

    results = await run_concurrently([start(i) for i in range(12)])

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, failures
    totals: Counter[str] = sum((r for r in results if isinstance(r, Counter)), Counter())
    # The workload must actually have exercised both success and refusal paths.
    assert totals["reserve"] > 20
    assert totals["settle"] + totals["release"] > 10
    assert totals["InsufficientCredits"] > 0

    for user in users:
        wallet = await wallet_state(session_factory, user)
        assert wallet.available_balance >= 0
        assert wallet.reserved_balance >= 0
        async with session_factory() as session:
            still_held = await session.scalar(
                select(func.coalesce(func.sum(WalletReservation.reserved_amount), 0)).where(
                    WalletReservation.user_id == user,
                    WalletReservation.status == ReservationStatus.ACTIVE.value,
                )
            )
        assert wallet.reserved_balance == still_held
    await assert_ledger_consistent(session_factory, clock)
