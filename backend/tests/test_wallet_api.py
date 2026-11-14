"""Wallet endpoints: read-only views over the ledger, in the user's own timezone."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import TransactionType
from app.seeds import seed_initial_configuration
from app.services.wallet import WalletService
from tests.conftest import AppFactory, ClientFactory
from tests.support import API, MutableClock, register_and_login

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]
TTL = timedelta(minutes=5)


async def seed(session_factory: Sessions) -> None:
    async with session_factory() as session, session.begin():
        await seed_initial_configuration(session)


async def fund(
    session_factory: Sessions, clock: MutableClock, user_id: uuid.UUID, amount: int
) -> None:
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).adjust(
            user_id,
            amount,
            kind=TransactionType.ADMIN_ADJUSTMENT,
            reason="test funding",
            actor_id=None,
        )


async def test_wallet_requires_a_session(client: httpx.AsyncClient) -> None:
    for path in ("/wallet", "/wallet/history", "/wallet/summary", "/wallet/reservations"):
        assert (await client.get(f"{API}{path}")).status_code == 401


async def test_new_wallet_is_empty(client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await seed(session_factory)
    await register_and_login(client)
    body = (await client.get(f"{API}/wallet")).json()

    assert body["available_wc"] == 0
    assert body["reserved_wc"] == 0
    assert body["lifetime_earned_wc"] == 0
    assert body["lifetime_spent_wc"] == 0
    assert body["available_eur"] == "0.0000"


async def test_wallet_shows_balances_and_the_eur_equivalent(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    """The specification's example: 3,300 WC available = EUR 0.33."""
    await seed(session_factory)
    account = await register_and_login(client)
    request_id, pending = uuid.uuid4(), uuid.uuid4()
    await fund(session_factory, clock, account.id, 2000)
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reward(account.id, 800, idempotency_key="mon-steps")
        await wallets.reward(account.id, 150, idempotency_key="mon-workout")
        await wallets.reward(account.id, 1000, idempotency_key="tue-steps")
        await wallets.reserve(account.id, 900, request_id=request_id, ttl=TTL)
        await wallets.settle(request_id, 650)

    response = await client.get(f"{API}/wallet")
    body = response.json()
    assert response.status_code == 200
    assert body["available_wc"] == 3300
    assert body["lifetime_earned_wc"] == 1950
    assert body["lifetime_spent_wc"] == 650
    # Money and rates are strings, never JSON floats.
    assert body["available_eur"] == "0.3300"
    assert body["wc_per_eur"] == "10000.00000000"
    assert '"available_eur":"0.3300"' in response.text.replace(" ", "")

    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(account.id, 300, request_id=pending, ttl=TTL)
    held = (await client.get(f"{API}/wallet")).json()
    assert (held["available_wc"], held["reserved_wc"], held["available_eur"]) == (
        3000,
        300,
        "0.3000",
    )


async def test_eur_equivalent_is_absent_until_a_rate_is_configured(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 500)
    body = (await client.get(f"{API}/wallet")).json()
    assert body["available_wc"] == 500
    assert body["wc_per_eur"] is None
    assert body["available_eur"] is None  # never a made-up conversion


async def test_eur_equivalent_rounds_down(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    from sqlalchemy import text

    account = await register_and_login(client)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO exchange_rates (base_currency, quote_currency, rate, effective_from) "
                "VALUES ('EUR', 'WC', 30000, '2026-01-01T00:00:00Z')"
            )
        )
    await fund(session_factory, clock, account.id, 20)  # 20 / 30000 = 0.000666...
    assert (await client.get(f"{API}/wallet")).json()["available_eur"] == "0.0006"


async def test_history_is_newest_first_paginated_and_filterable(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 1000)
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        for day in range(1, 8):
            await wallets.reward(
                account.id, day * 10, idempotency_key=f"day-{day}", description=f"day {day}"
            )

    first = (await client.get(f"{API}/wallet/history", params={"limit": 3})).json()
    second = (
        await client.get(
            f"{API}/wallet/history", params={"limit": 3, "cursor": first["next_cursor"]}
        )
    ).json()
    rest = (
        await client.get(
            f"{API}/wallet/history", params={"limit": 3, "cursor": second["next_cursor"]}
        )
    ).json()

    assert [t["description"] for t in first["items"]] == ["day 7", "day 6", "day 5"]
    assert [t["description"] for t in second["items"]] == ["day 4", "day 3", "day 2"]
    assert [t["description"] for t in rest["items"]] == ["day 1", "test funding"]
    assert rest["next_cursor"] is None
    newest = first["items"][0]
    assert newest == {
        "id": newest["id"],
        "type": "ACTIVITY_REWARD",
        "amount_wc": 70,
        "reserved_delta_wc": 0,
        "balance_after_wc": 1280,
        "reserved_after_wc": 0,
        "description": "day 7",
        "reference_type": None,
        "reference_id": None,
        "created_at": newest["created_at"],
    }

    adjustments = (
        await client.get(f"{API}/wallet/history", params={"type": "ADMIN_ADJUSTMENT"})
    ).json()
    assert [t["amount_wc"] for t in adjustments["items"]] == [1000]


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 201}, {"cursor": 0}, {"cursor": "abc"}, {"type": "FREE_MONEY"}],
)
async def test_history_rejects_invalid_parameters(
    client: httpx.AsyncClient, params: dict[str, str | int]
) -> None:
    await register_and_login(client)
    assert (await client.get(f"{API}/wallet/history", params=params)).status_code == 422


async def test_users_only_ever_see_their_own_wallet(
    make_app: AppFactory, make_client: ClientFactory, clock: MutableClock
) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as alice,
        make_client(app) as bob,
    ):
        alice_account = await register_and_login(alice, "alice@example.com")
        await register_and_login(bob, "bob@example.com")
        request_id = uuid.uuid4()
        async with app.state.session_factory() as session, session.begin():
            wallets = WalletService(session, clock)
            await wallets.reward(alice_account.id, 900, idempotency_key="alice-day")
            await wallets.reserve(alice_account.id, 100, request_id=request_id, ttl=TTL)

        assert (await alice.get(f"{API}/wallet")).json()["available_wc"] == 800
        assert (await bob.get(f"{API}/wallet")).json()["available_wc"] == 0
        assert (await bob.get(f"{API}/wallet/history")).json()["items"] == []
        assert (await bob.get(f"{API}/wallet/reservations")).json() == []
        bob_summary = (await bob.get(f"{API}/wallet/summary")).json()
        assert bob_summary["today"]["earned_wc"] == 0
        assert len((await alice.get(f"{API}/wallet/reservations")).json()) == 1


async def test_summary_uses_lisbon_midnight_not_utc_midnight(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    """In summer Lisbon is UTC+1: the day rolls over at 23:00 UTC."""
    account = await register_and_login(client)

    async def reward_at(instant: datetime, amount: int, key: str) -> None:
        clock.set(instant)
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reward(account.id, amount, idempotency_key=key)

    await reward_at(datetime(2026, 7, 14, 22, 30, tzinfo=UTC), 400, "14th-late")  # 23:30 local
    await reward_at(datetime(2026, 7, 14, 23, 15, tzinfo=UTC), 250, "15th-early")  # 00:15 local

    clock.set(datetime(2026, 7, 14, 23, 30, tzinfo=UTC))  # 00:30 on the 15th in Lisbon
    summary = (await client.get(f"{API}/wallet/summary")).json()

    assert summary["timezone"] == "Europe/Lisbon"
    assert summary["today"] == {
        "date": "2026-07-15",
        "earned_wc": 250,
        "spent_wc": 0,
        "adjustments_wc": 0,
    }
    assert summary["month"] == {
        "month": "2026-07",
        "earned_wc": 650,
        "spent_wc": 0,
        "adjustments_wc": 0,
    }

    # Five minutes before local midnight it is still the 14th.
    clock.set(datetime(2026, 7, 14, 22, 55, tzinfo=UTC))
    earlier = (await client.get(f"{API}/wallet/summary")).json()
    assert (earlier["today"]["date"], earlier["today"]["earned_wc"]) == ("2026-07-14", 400)


async def test_summary_winter_day_and_month_boundaries(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    request_id = uuid.uuid4()

    async def at(instant: datetime) -> WalletService:
        clock.set(instant)
        return WalletService(session, clock)

    async with session_factory() as session, session.begin():
        # 30 June 23:30 UTC is already 1 July in Lisbon: it belongs to July.
        await (await at(datetime(2026, 6, 30, 23, 30, tzinfo=UTC))).reward(
            account.id, 100, idempotency_key="july-first-minutes"
        )
        # 31 July 23:30 UTC is 1 August in Lisbon: it does not.
        await (await at(datetime(2026, 7, 31, 23, 30, tzinfo=UTC))).reward(
            account.id, 900, idempotency_key="august"
        )
        wallets = await at(datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
        await wallets.reward(account.id, 500, idempotency_key="july-20")
        await wallets.reserve(account.id, 300, request_id=request_id, ttl=TTL)
        await wallets.settle(request_id, 120)
        await wallets.adjust(
            account.id,
            -50,
            kind=TransactionType.ADMIN_ADJUSTMENT,
            reason="correction",
            actor_id=None,
        )

    clock.set(datetime(2026, 7, 20, 18, 0, tzinfo=UTC))
    summary = (await client.get(f"{API}/wallet/summary")).json()

    assert summary["today"] == {
        "date": "2026-07-20",
        "earned_wc": 500,
        "spent_wc": 120,
        "adjustments_wc": -50,
    }
    assert summary["month"] == {
        "month": "2026-07",
        "earned_wc": 600,  # 100 + 500; the August reward is excluded
        "spent_wc": 120,
        "adjustments_wc": -50,
    }


async def test_summary_follows_the_users_own_timezone(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    clock.set(datetime(2026, 7, 15, 2, 0, tzinfo=UTC))  # 03:00 in Lisbon, 22:00 (14th) in New York
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reward(account.id, 300, idempotency_key="k")
    await client.post(f"{API}/auth/refresh")

    assert (await client.get(f"{API}/wallet/summary")).json()["today"]["date"] == "2026-07-15"
    await client.patch(f"{API}/auth/me", json={"timezone": "America/New_York"})
    summary = (await client.get(f"{API}/wallet/summary")).json()
    assert summary["timezone"] == "America/New_York"
    assert (summary["today"]["date"], summary["today"]["earned_wc"]) == ("2026-07-14", 300)


async def test_reservations_listing(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 1000)
    settled, active = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reserve(account.id, 200, request_id=settled, ttl=TTL, description="first")
        await wallets.settle(settled, 150)
        clock.advance(seconds=5)
        await wallets.reserve(account.id, 300, request_id=active, ttl=TTL, description="second")

    everything = (await client.get(f"{API}/wallet/reservations")).json()
    only_active = (
        await client.get(f"{API}/wallet/reservations", params={"active_only": "true"})
    ).json()

    assert [(r["description"], r["status"]) for r in everything] == [
        ("second", "active"),
        ("first", "settled"),
    ]
    assert everything[1]["reserved_wc"] == 200
    assert (everything[1]["settled_wc"], everything[1]["released_wc"]) == (150, 50)
    assert [r["request_id"] for r in only_active] == [str(active)]
    assert only_active[0]["closed_at"] is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path", ["/wallet", "/wallet/history", "/wallet/summary", "/wallet/reservations"]
)
async def test_no_wallet_route_can_change_a_balance(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    method: str,
    path: str,
) -> None:
    """There is no endpoint through which a client can set, add or move credits."""
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 100)

    response = await client.request(
        method, f"{API}{path}", json={"available_wc": 10**9, "amount_wc": 10**9}
    )

    assert response.status_code == 405
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 100
