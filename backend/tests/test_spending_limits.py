"""Spending limits: per-request, daily, monthly and minimum balance — enforced when
credits are reserved, in the user's own timezone, safely under concurrency."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.wallet import (
    InsufficientCredits,
    SpendingLimitExceeded,
    WalletService,
)
from app.services.wallet.limits import replace_limits
from tests.conftest import AppFactory, ClientFactory
from tests.support import (
    API,
    MutableClock,
    assert_ledger_consistent,
    create_user_with_wallet,
    register_and_login,
    wallet_state,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]
TTL = timedelta(minutes=5)


async def set_limits(
    session_factory: Sessions,
    clock: MutableClock,
    user: uuid.UUID,
    *,
    request: int | None = None,
    daily: int | None = None,
    monthly: int | None = None,
    min_balance: int = 0,
) -> None:
    async with session_factory() as session, session.begin():
        await replace_limits(
            session,
            user,
            clock.now(),
            max_request_wc=request,
            max_daily_wc=daily,
            max_monthly_wc=monthly,
            min_balance_wc=min_balance,
            alert_threshold_pct=80,
        )


async def reserve(
    session_factory: Sessions, clock: MutableClock, user: uuid.UUID, amount: int
) -> uuid.UUID:
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, amount, request_id=request_id, ttl=TTL)
    return request_id


async def spend(
    session_factory: Sessions, clock: MutableClock, user: uuid.UUID, amount: int
) -> None:
    """A complete AI request: reserve the worst case, then settle the actual cost."""
    request_id = await reserve(session_factory, clock, user, amount)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).settle(request_id, amount)


# ----------------------------------------------------------------- each limit


async def test_without_limits_only_the_balance_bounds_spending(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    await spend(session_factory, clock, user, 1000)
    with pytest.raises(InsufficientCredits):
        await reserve(session_factory, clock, user, 1)


async def test_per_request_limit(session_factory: Sessions, clock: MutableClock) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=5000)
    await set_limits(session_factory, clock, user, request=400)

    await reserve(session_factory, clock, user, 400)  # exactly at the limit: allowed
    with pytest.raises(SpendingLimitExceeded) as excinfo:
        await reserve(session_factory, clock, user, 401)

    assert excinfo.value.status_code == 402
    assert excinfo.value.code == "spending_limit_exceeded"
    assert excinfo.value.extra == {"limit": "per_request", "requested_wc": 401, "limit_wc": 400}
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (4600, 400)


async def test_daily_limit_counts_what_was_spent_and_what_is_held(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=5000)
    await set_limits(session_factory, clock, user, daily=1000)

    await spend(session_factory, clock, user, 600)  # spent today: 600
    held = await reserve(session_factory, clock, user, 300)  # in flight: 300
    with pytest.raises(SpendingLimitExceeded) as excinfo:
        await reserve(session_factory, clock, user, 101)  # 600 + 300 + 101 > 1,000
    assert excinfo.value.extra == {
        "limit": "daily",
        "requested_wc": 101,
        "limit_wc": 1000,
        "used_wc": 900,
    }
    await reserve(session_factory, clock, user, 100)  # exactly reaches the limit

    # A request that fails gives its allowance back along with its credits.
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).release(held)
    await reserve(session_factory, clock, user, 300)
    await assert_ledger_consistent(session_factory, clock)


async def test_only_actual_cost_counts_not_the_worst_case_reservation(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=5000)
    await set_limits(session_factory, clock, user, daily=1000)

    request_id = await reserve(session_factory, clock, user, 900)  # worst-case estimate
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).settle(request_id, 150)  # what it really cost

    await reserve(session_factory, clock, user, 850)  # 150 spent, so 850 is still free
    with pytest.raises(SpendingLimitExceeded):
        await reserve(session_factory, clock, user, 1)


async def test_daily_limit_resets_at_lisbon_midnight_not_utc_midnight(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """In summer Lisbon's day ends at 23:00 UTC."""
    user = await create_user_with_wallet(session_factory, clock, balance=5000)
    await set_limits(session_factory, clock, user, daily=500)

    clock.set(datetime(2026, 7, 14, 22, 30, tzinfo=UTC))  # 23:30 local, 14 July
    await spend(session_factory, clock, user, 500)
    with pytest.raises(SpendingLimitExceeded):
        await reserve(session_factory, clock, user, 1)

    clock.set(datetime(2026, 7, 14, 23, 5, tzinfo=UTC))  # 00:05 local, 15 July
    await spend(session_factory, clock, user, 500)  # a new local day: a fresh allowance

    clock.set(datetime(2026, 7, 15, 0, 30, tzinfo=UTC))  # past UTC midnight, same local day
    with pytest.raises(SpendingLimitExceeded):
        await reserve(session_factory, clock, user, 1)


async def test_monthly_limit_spans_days_and_resets_with_the_local_month(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=9000)
    await set_limits(session_factory, clock, user, daily=600, monthly=1500)

    for day_of_month in (10, 11):
        clock.set(datetime(2026, 7, day_of_month, 12, 0, tzinfo=UTC))
        await spend(session_factory, clock, user, 600)
    clock.set(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    with pytest.raises(SpendingLimitExceeded) as excinfo:
        await reserve(session_factory, clock, user, 301)  # within today's limit, not the month's
    assert excinfo.value.extra["limit"] == "monthly"
    await spend(session_factory, clock, user, 300)

    clock.set(datetime(2026, 7, 31, 22, 30, tzinfo=UTC))  # still July in Lisbon
    with pytest.raises(SpendingLimitExceeded):
        await reserve(session_factory, clock, user, 1)
    clock.set(datetime(2026, 7, 31, 23, 30, tzinfo=UTC))  # 00:30 on 1 August in Lisbon
    await spend(session_factory, clock, user, 600)


async def test_minimum_balance_is_kept_in_reserve(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    await set_limits(session_factory, clock, user, min_balance=250)

    with pytest.raises(SpendingLimitExceeded) as excinfo:
        await reserve(session_factory, clock, user, 751)
    assert excinfo.value.extra == {
        "limit": "min_balance",
        "requested_wc": 751,
        "limit_wc": 250,
        "available_wc": 1000,
    }
    await reserve(session_factory, clock, user, 750)
    assert (await wallet_state(session_factory, user)).available_balance == 250


async def test_an_empty_wallet_is_reported_as_insufficient_credits_not_as_a_limit(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """The user should be told to go and earn credits, not to raise a limit."""
    user = await create_user_with_wallet(session_factory, clock, balance=100)
    await set_limits(session_factory, clock, user, request=50, daily=50)
    with pytest.raises(InsufficientCredits):
        await reserve(session_factory, clock, user, 500)


async def test_limits_cannot_be_dodged_by_simultaneous_requests(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Plenty of balance, a 500 WC daily limit, ten requests of 200 WC at once."""
    user = await create_user_with_wallet(session_factory, clock, balance=10000)
    await set_limits(session_factory, clock, user, daily=500)

    async def attempt() -> str:
        try:
            await reserve(session_factory, clock, user, 200)
        except SpendingLimitExceeded:
            return "refused"
        return "reserved"

    async with asyncio.timeout(60):
        outcomes = await asyncio.gather(*(attempt() for _ in range(10)))

    assert sorted(outcomes) == ["refused"] * 8 + ["reserved"] * 2
    assert (await wallet_state(session_factory, user)).reserved_balance == 400
    await assert_ledger_consistent(session_factory, clock)


async def test_limits_do_not_restrict_earning(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    await set_limits(session_factory, clock, user, request=1, daily=1, monthly=1, min_balance=5000)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reward(user, 1500, idempotency_key="day")
    assert (await wallet_state(session_factory, user)).available_balance == 1500


# ------------------------------------------------------------------------- API


async def limits(client: httpx.AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"{API}/wallet/limits")).json()
    return body


async def test_limits_default_to_none(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API}/wallet/limits")).status_code == 401
    await register_and_login(client)
    current = await limits(client)
    assert {k: v for k, v in current.items() if k != "updated_at"} == {
        "max_request_wc": None,
        "max_daily_wc": None,
        "max_monthly_wc": None,
        "min_balance_wc": 0,
        "alert_threshold_pct": 80,
    }


async def test_limits_can_be_set_replaced_and_cleared_and_changes_are_audited(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await register_and_login(client)

    first = await client.put(
        f"{API}/wallet/limits",
        json={"max_request_wc": 300, "max_daily_wc": 1000, "max_monthly_wc": 20000},
    )
    unchanged = await client.put(
        f"{API}/wallet/limits",
        json={"max_request_wc": 300, "max_daily_wc": 1000, "max_monthly_wc": 20000},
    )
    replaced = await client.put(
        f"{API}/wallet/limits",
        json={"max_daily_wc": 800, "min_balance_wc": 100, "alert_threshold_pct": 50},
    )

    assert (first.status_code, unchanged.status_code, replaced.status_code) == (200, 200, 200)
    assert {k: v for k, v in replaced.json().items() if k != "updated_at"} == {
        "max_request_wc": None,  # PUT replaces everything: an omitted limit is removed
        "max_daily_wc": 800,
        "max_monthly_wc": None,
        "min_balance_wc": 100,
        "alert_threshold_pct": 50,
    }
    async with session_factory() as session:
        audited = (
            await session.scalars(
                text(
                    "SELECT details FROM audit_log WHERE action = 'wallet.limits_changed' "
                    "ORDER BY id"
                )
            )
        ).all()
    assert len(audited) == 2  # the identical PUT changed nothing and was not logged
    assert audited[0] == {
        "max_request_wc": {"from": None, "to": 300},
        "max_daily_wc": {"from": None, "to": 1000},
        "max_monthly_wc": {"from": None, "to": 20000},
    }
    assert audited[1]["max_daily_wc"] == {"from": 1000, "to": 800}


@pytest.mark.parametrize(
    "body",
    [
        {"max_daily_wc": 0},
        {"max_request_wc": -5},
        {"max_daily_wc": 1.5},
        {"min_balance_wc": -1},
        {"alert_threshold_pct": 0},
        {"alert_threshold_pct": 101},
        {"max_daily_wc": 5000, "max_monthly_wc": 1000},  # a day cannot exceed its month
        {"max_request_wc": 900, "max_daily_wc": 500},  # nor a request its day
        {"max_daily_wc": 500, "available_wc": 99999},  # no balance can be smuggled in
    ],
)
async def test_incoherent_limits_are_rejected(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    await register_and_login(client)
    assert (await client.put(f"{API}/wallet/limits", json=body)).status_code == 422
    assert (await limits(client))["max_daily_wc"] is None


async def test_summary_shows_usage_headroom_and_alerts(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    await client.post(
        f"{API}/admin/wallet/adjustments",
        json={"user_id": str(account.id), "amount_wc": 5000, "reason": "test funding"},
    )

    unlimited = (await client.get(f"{API}/wallet/summary")).json()["spending"]
    assert unlimited["daily"] == {
        "limit_wc": None,
        "used_wc": 0,
        "held_wc": 0,
        "remaining_wc": None,
        "alert": False,
    }

    await client.put(
        f"{API}/wallet/limits",
        json={"max_request_wc": 400, "max_daily_wc": 1000, "max_monthly_wc": 3000},
    )
    await spend(session_factory, clock, account.id, 400)
    await spend(session_factory, clock, account.id, 300)
    await reserve(session_factory, clock, account.id, 150)

    spending = (await client.get(f"{API}/wallet/summary")).json()["spending"]
    assert spending == {
        "max_request_wc": 400,
        "min_balance_wc": 0,
        "alert_threshold_pct": 80,
        "daily": {
            "limit_wc": 1000,
            "used_wc": 700,
            "held_wc": 150,
            "remaining_wc": 150,
            "alert": True,  # 850 of 1,000 is past the 80% threshold
        },
        "monthly": {
            "limit_wc": 3000,
            "used_wc": 700,
            "held_wc": 150,
            "remaining_wc": 2150,
            "alert": False,
        },
    }


async def test_limits_belong_to_each_user(make_app: AppFactory, make_client: ClientFactory) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as alice,
        make_client(app) as bob,
    ):
        await register_and_login(alice, "alice@example.com")
        await register_and_login(bob, "bob@example.com")
        await alice.put(f"{API}/wallet/limits", json={"max_daily_wc": 100})
        assert (await limits(alice))["max_daily_wc"] == 100
        assert (await limits(bob))["max_daily_wc"] is None
