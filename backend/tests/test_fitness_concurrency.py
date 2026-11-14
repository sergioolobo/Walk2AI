"""Concurrency: simultaneous synchronisations must never reward the same activity twice.

Requests are issued together over HTTP; each is served on its own database connection
and transaction, so they genuinely contend for the user's wallet lock.
"""

import asyncio
import random
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.wallet import InsufficientCredits, WalletService
from tests.support import (
    API,
    MutableClock,
    assert_ledger_consistent,
    day,
    pair_device,
    register_and_login,
    seed_configuration,
    workout,
)

pytestmark = [pytest.mark.anyio, pytest.mark.concurrency]

Sessions = async_sessionmaker[AsyncSession]

TODAY = "2026-11-13"
TIMEOUT = 60


@pytest.fixture
async def device(client: httpx.AsyncClient, session_factory: Sessions) -> str:
    await seed_configuration(session_factory)
    await register_and_login(client)
    return await pair_device(client)


async def post_sync(
    client: httpx.AsyncClient, token: str, batch_id: str | None = None, **content: Any
) -> httpx.Response:
    return await client.post(
        f"{API}/fitness/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={"batch_id": batch_id or str(uuid.uuid4()), **content},
    )


async def together(*requests: Any) -> list[Any]:
    async with asyncio.timeout(TIMEOUT):
        return list(await asyncio.gather(*requests))


async def balance(client: httpx.AsyncClient) -> int:
    available: int = (await client.get(f"{API}/wallet")).json()["available_wc"]
    return available


async def ledger_rows(session_factory: Sessions) -> int:
    async with session_factory() as session:
        return int(await session.scalar(text("SELECT count(*) FROM wallet_transactions")) or 0)


async def test_the_same_batch_sent_many_times_at_once_is_processed_once(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """A device retrying an upload it believes failed, twelve times over."""
    batch_id = str(uuid.uuid4())
    content = {
        "days": [day(TODAY, 8000)],
        "workouts": [workout("S", "traditional_strength_training", f"{TODAY}T07:00:00+00:00", 30)],
    }

    responses = await together(*(post_sync(client, device, batch_id, **content) for _ in range(12)))

    assert {r.status_code for r in responses} == {200}
    assert sorted(r.json()["replayed"] for r in responses) == [False] + [True] * 11
    assert {r.json()["rewards"][0]["awarded_total_wc"] for r in responses} == {950}
    assert await balance(client) == 950
    assert await ledger_rows(session_factory) == 1
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM fitness_sync_batches")) == 1
        assert await session.scalar(text("SELECT count(*) FROM workout_events")) == 1
    await assert_ledger_consistent(session_factory, clock)


async def test_many_different_batches_for_the_same_day_pay_it_once(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Two devices, or one device syncing from several triggers at the same moment."""
    session = workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)

    responses = await together(
        *(post_sync(client, device, days=[day(TODAY, 6000)], workouts=[session]) for _ in range(15))
    )

    assert {r.status_code for r in responses} == {200}
    paid = sorted(r.json()["rewards"][0]["awarded_now_wc"] for r in responses)
    assert paid == [0] * 14 + [800]  # exactly one request paid the day: 600 + 200
    assert await balance(client) == 800
    assert await ledger_rows(session_factory) == 1
    await assert_ledger_consistent(session_factory, clock)


async def test_racing_updates_of_a_growing_day_end_at_the_highest_total(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Whatever order the database serves them in, the day ends paid for its best total
    and not a credit more."""
    step_counts = [1200, 9400, 3000, 7700, 500, 9400, 6100, 8800, 2500, 4400]

    responses = await together(
        *(post_sync(client, device, days=[day(TODAY, steps)]) for steps in step_counts)
    )

    assert {r.status_code for r in responses} == {200}
    assert sum(r.json()["rewards"][0]["awarded_now_wc"] for r in responses) == 940
    assert await balance(client) == 940
    await assert_ledger_consistent(session_factory, clock)


async def test_several_days_synchronised_at_once_each_respect_their_own_cap(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    dates = [f"2026-11-{d:02d}" for d in range(7, 14)]

    responses = await together(
        *(post_sync(client, device, days=[day(date_, 40000) for date_ in dates]) for _ in range(6))
    )

    assert {r.status_code for r in responses} == {200}
    assert await balance(client) == 7 * 1500
    assert await ledger_rows(session_factory) == 7
    await assert_ledger_consistent(session_factory, clock)


async def test_earning_from_syncs_while_spending_stays_consistent(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Syncs paying credits in while requests reserve and settle credits out."""
    user_id = uuid.UUID((await client.get(f"{API}/auth/me")).json()["id"])
    await post_sync(client, device, days=[day("2026-11-12", 10000)])  # 1,000 WC to start with

    async def spend() -> int:
        request_id = uuid.uuid4()
        try:
            async with session_factory() as session, session.begin():
                await WalletService(session, clock).reserve(
                    user_id, 300, request_id=request_id, ttl=timedelta(minutes=5)
                )
            async with session_factory() as session, session.begin():
                return (await WalletService(session, clock).settle(request_id, 220)).charged
        except InsufficientCredits:
            return 0

    results = await together(
        *(post_sync(client, device, days=[day(TODAY, 1000 * n)]) for n in range(1, 9)),
        *(spend() for _ in range(8)),
    )

    spent = sum(r for r in results if isinstance(r, int))
    assert spent > 0
    wallet = (await client.get(f"{API}/wallet")).json()
    assert wallet["lifetime_earned_wc"] == 1000 + 800
    assert wallet["lifetime_spent_wc"] == spent
    assert wallet["available_wc"] == 1800 - spent
    assert wallet["reserved_wc"] == 0
    await assert_ledger_consistent(session_factory, clock)


async def test_excluding_a_workout_while_it_is_being_synchronised(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    session = workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)
    await post_sync(client, device, workouts=[session])
    identifier = (await client.get(f"{API}/fitness/workouts")).json()[0]["id"]

    async def toggle(excluded: bool) -> httpx.Response:
        return await client.patch(
            f"{API}/fitness/workouts/{identifier}", json={"excluded_from_rewards": excluded}
        )

    responses = await together(
        *(post_sync(client, device, workouts=[session]) for _ in range(6)),
        *(toggle(flag) for flag in (True, False, True, False, True)),
    )

    assert {r.status_code for r in responses} == {200}
    final = (await client.get(f"{API}/fitness/workouts")).json()[0]
    # Whichever toggle ran last decides; the wallet agrees with it exactly.
    assert await balance(client) == (0 if final["excluded_from_rewards"] else 200)
    await assert_ledger_consistent(session_factory, clock)


async def test_randomised_concurrent_syncs_never_overpay(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """A fixed-seed storm of overlapping syncs across a week. Afterwards every day has
    been paid at most its cap, and exactly what its final state — or an earlier, larger
    one — was worth."""
    rng = random.Random(20261114)  # noqa: S311 - reproducible test data, not security
    dates = [f"2026-11-{d:02d}" for d in range(8, 14)]
    highest: dict[str, int] = dict.fromkeys(dates, 0)
    requests = []
    for _ in range(40):
        chosen = rng.sample(dates, rng.randint(1, 4))
        days = []
        for date_ in chosen:
            steps = rng.randint(0, 22000)
            highest[date_] = max(highest[date_], steps)
            days.append(day(date_, steps))
        requests.append(post_sync(client, device, days=days))

    responses = await together(*requests)

    assert {r.status_code for r in responses} == {200}
    async with session_factory() as session:
        awarded: dict[str, int] = dict(
            (
                await session.execute(
                    text("SELECT activity_date::text, credits_awarded FROM daily_rewards")
                )
            ).all()
        )
    for date_ in dates:
        best = min(1500, highest[date_] // 10)
        assert awarded[date_] == best, date_  # paid for its best total, never more
    assert await balance(client) == sum(awarded.values())
    await assert_ledger_consistent(session_factory, clock)
