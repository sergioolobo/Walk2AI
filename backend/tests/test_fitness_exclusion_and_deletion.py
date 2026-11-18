"""Manual exclusion of workouts (the only path that takes credits back) and deletion of
stored fitness data (which must never make a day payable twice)."""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.wallet import WalletService
from tests.conftest import AppFactory, ClientFactory
from tests.support import (
    API,
    MutableClock,
    assert_ledger_consistent,
    day,
    pair_device,
    register_and_login,
    seed_configuration,
    sync,
    workout,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

TODAY = "2026-11-13"


@pytest.fixture
async def device(client: httpx.AsyncClient, session_factory: Sessions) -> str:
    await seed_configuration(session_factory)
    await register_and_login(client)
    return await pair_device(client)


async def wallet(client: httpx.AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"{API}/wallet")).json()
    return body


async def workout_id(client: httpx.AsyncClient, external_id: str) -> str:
    listed = (await client.get(f"{API}/fitness/workouts")).json()
    found: str = next(w["id"] for w in listed if w["external_id"] == external_id)
    return found


async def exclude(
    client: httpx.AsyncClient, identifier: str, excluded: bool = True, reason: str | None = None
) -> httpx.Response:
    return await client.patch(
        f"{API}/fitness/workouts/{identifier}",
        json={"excluded_from_rewards": excluded, "reason": reason},
    )


async def spend(session_factory: Sessions, clock: MutableClock, user_id: str, amount: int) -> None:
    """Spend credits the way the AI gateway will: reserve, then settle."""
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reserve(
            uuid.UUID(user_id), amount, request_id=request_id, ttl=timedelta(minutes=5)
        )
        await wallets.settle(request_id, amount)


# -------------------------------------------------------------------- exclusion


async def test_excluding_a_rewarded_workout_reverses_its_credits(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    await sync(
        client,
        device,
        days=[day(TODAY, 8000)],
        workouts=[
            workout("STRENGTH", "traditional_strength_training", f"{TODAY}T07:00:00+00:00", 30)
        ],
    )
    assert (await wallet(client))["available_wc"] == 950
    identifier = await workout_id(client, "STRENGTH")

    response = await exclude(client, identifier, reason="Left the watch running by mistake")

    assert response.status_code == 200
    body = response.json()
    assert body["workout"]["excluded_from_rewards"] is True
    assert body["workout"]["exclusion_reason"] == "Left the watch running by mistake"
    assert (body["workout"]["earns_minutes"], body["workout"]["reason"]) == (False, "excluded")
    reward = body["rewards"][0]
    assert (reward["eligible_minutes"], reward["entitled_wc"]) == (0, 800)
    assert (reward["reversed_now_wc"], reward["awarded_total_wc"]) == (150, 800)
    assert body["wallet"] == {"available_wc": 800, "reserved_wc": 0}

    state = await wallet(client)
    assert (state["available_wc"], state["lifetime_earned_wc"]) == (800, 800)
    newest = (await client.get(f"{API}/wallet/history")).json()["items"][0]
    assert (newest["type"], newest["amount_wc"]) == ("REWARD_REVERSAL", -150)
    assert newest["description"] == f"Reward reversed for {TODAY}"
    async with session_factory() as session:
        audit = await session.scalar(
            text("SELECT details FROM audit_log WHERE action = 'fitness.workout_excluded'")
        )
    assert audit == {
        "reason": "Left the watch running by mistake",
        "reversed_wc": 150,
        "awarded_wc": 0,
    }
    await assert_ledger_consistent(session_factory, clock)


async def test_including_a_workout_again_pays_it_again_exactly_once(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Exclude and include repeatedly: the wallet always ends where the facts say."""
    await sync(client, device, workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)])
    identifier = await workout_id(client, "C")

    balances = []
    for excluded in (True, False, True, False, False, True, True, False):
        response = await exclude(client, identifier, excluded)
        assert response.status_code == 200
        balances.append(response.json()["wallet"]["available_wc"])

    assert balances == [0, 200, 0, 200, 200, 0, 0, 200]
    state = await wallet(client)
    assert (state["available_wc"], state["lifetime_earned_wc"]) == (200, 200)
    kinds = [t["type"] for t in (await client.get(f"{API}/wallet/history")).json()["items"]]
    assert kinds.count("ACTIVITY_REWARD") == 4  # the original payment and three re-inclusions
    assert kinds.count("REWARD_REVERSAL") == 3
    await assert_ledger_consistent(session_factory, clock)


async def test_setting_the_same_state_twice_changes_nothing(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(client, device, workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)])
    identifier = await workout_id(client, "C")

    already_included = await exclude(client, identifier, excluded=False)
    assert already_included.json()["rewards"] == []
    await exclude(client, identifier)
    again = await exclude(client, identifier)
    assert again.json()["rewards"] == []
    assert (await wallet(client))["available_wc"] == 0
    assert len((await client.get(f"{API}/wallet/history")).json()["items"]) == 2


async def test_a_reversal_never_takes_the_balance_below_zero(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Credits that were already spent cannot be clawed back: the wallet stops at zero,
    and the uncollected part is not earned a second time later."""
    await sync(client, device, workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 60)])
    me = (await client.get(f"{API}/auth/me")).json()
    await spend(session_factory, clock, me["id"], 250)  # 300 earned, 50 left
    identifier = await workout_id(client, "C")

    response = await exclude(client, identifier)

    reward = response.json()["rewards"][0]
    assert reward["entitled_wc"] == 0
    assert reward["reversed_now_wc"] == 50  # all that could be collected
    assert reward["awarded_total_wc"] == 250  # the spent part stays on the day's record
    assert response.json()["wallet"]["available_wc"] == 0

    # Including it again pays only what was actually taken back, not the full 300.
    restored = await exclude(client, identifier, excluded=False)
    assert restored.json()["rewards"][0]["awarded_now_wc"] == 50
    state = await wallet(client)
    assert (state["available_wc"], state["lifetime_earned_wc"]) == (50, 300)
    await assert_ledger_consistent(session_factory, clock)


async def test_excluding_a_workout_on_a_capped_day_takes_nothing_back(
    client: httpx.AsyncClient, device: str
) -> None:
    """20,000 steps already reach the 1,500 cap: the workout never added anything."""
    await sync(
        client,
        device,
        days=[day(TODAY, 20000)],
        workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 60)],
    )
    response = await exclude(client, await workout_id(client, "C"))
    assert response.json()["rewards"][0]["reversed_now_wc"] == 0
    assert (await wallet(client))["available_wc"] == 1500


async def test_excluding_a_workout_across_midnight_adjusts_both_days(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(
        client, device, workouts=[workout("LATE", "cycling", "2026-11-12T23:40:00+00:00", 40)]
    )
    assert (await wallet(client))["available_wc"] == 200
    response = await exclude(client, await workout_id(client, "LATE"))
    reversed_by_day = {r["date"]: r["reversed_now_wc"] for r in response.json()["rewards"]}
    assert reversed_by_day == {"2026-11-12": 100, "2026-11-13": 100}
    assert (await wallet(client))["available_wc"] == 0


async def test_an_excluded_workout_stays_excluded_when_it_is_synchronised_again(
    client: httpx.AsyncClient, device: str
) -> None:
    session = workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)
    await sync(client, device, workouts=[session])
    await exclude(client, await workout_id(client, "C"), reason="not real")

    resent = await sync(client, device, workouts=[session])

    assert resent["workouts"][0]["earns_minutes"] is False
    assert resent["rewards"][0]["awarded_now_wc"] == 0
    assert (await wallet(client))["available_wc"] == 0
    listed = (await client.get(f"{API}/fitness/workouts")).json()
    assert (listed[0]["excluded_from_rewards"], listed[0]["exclusion_reason"]) == (True, "not real")


async def test_only_the_owner_can_exclude_a_workout(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as owner,
        make_client(app) as stranger,
        make_client(app) as anonymous,
    ):
        await seed_configuration(app.state.session_factory)
        await register_and_login(owner, "owner@example.com")
        await register_and_login(stranger, "stranger@example.com")
        token = await pair_device(owner)
        await sync(owner, token, workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 40)])
        identifier = await workout_id(owner, "C")

        assert (await exclude(stranger, identifier)).status_code == 404
        assert (await exclude(anonymous, identifier)).status_code == 401
        with_device_token = await anonymous.patch(
            f"{API}/fitness/workouts/{identifier}",
            json={"excluded_from_rewards": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert with_device_token.status_code == 401  # a device cannot edit, only report
        assert (await exclude(owner, str(uuid.uuid4()))).status_code == 404
        assert (await exclude(owner, "not-a-uuid")).status_code == 422
        assert (await wallet(owner))["available_wc"] == 200


# --------------------------------------------------------------------- deletion


async def test_deleting_fitness_data_keeps_credits_and_cannot_be_used_to_earn_twice(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    activity: dict[str, Any] = {
        "days": [day("2026-11-12", 9000), day(TODAY, 4000)],
        "workouts": [workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 30)],
    }
    await sync(client, device, **activity)
    assert (await wallet(client))["available_wc"] == 900 + 400 + 150

    response = await client.delete(f"{API}/fitness/data")

    assert response.status_code == 200
    assert response.json() == {"days": 2, "workouts": 1, "sync_batches": 1}
    assert (await wallet(client))["available_wc"] == 1450  # earned credits are untouched
    assert (await client.get(f"{API}/fitness/workouts")).json() == []
    kept = (
        await client.get(f"{API}/fitness/history", params={"from": "2026-11-01", "to": TODAY})
    ).json()
    # Only the date and the credits awarded remain: no steps, no distances, no minutes.
    assert [(d["date"], d["steps"], d["eligible_minutes"], d["awarded_wc"]) for d in kept] == [
        (TODAY, 0, 0, 550),
        ("2026-11-12", 0, 0, 900),
    ]
    async with session_factory() as session:
        for table in ("fitness_daily", "workout_events", "fitness_sync_batches"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        audit = await session.scalar(
            text("SELECT details FROM audit_log WHERE action = 'fitness.data_deleted'")
        )
    assert audit == {"days": 2, "workouts": 1, "sync_batches": 1}

    # The same activity synchronised again is stored again — and pays nothing.
    again = await sync(client, device, **activity)
    assert {r["date"]: r["awarded_now_wc"] for r in again["rewards"]} == {
        "2026-11-12": 0,
        TODAY: 0,
    }
    assert (await wallet(client))["available_wc"] == 1450
    # Genuinely new activity on those days still earns the difference.
    more = await sync(client, device, days=[day(TODAY, 6000)])
    assert more["rewards"][0]["awarded_now_wc"] == 200
    await assert_ledger_consistent(session_factory, clock)


async def test_deleting_fitness_data_requires_the_users_own_session(
    client: httpx.AsyncClient, device: str, make_client: ClientFactory
) -> None:
    await sync(client, device, days=[day(TODAY, 4000)])
    async with make_client() as other:
        anonymous = await other.delete(f"{API}/fitness/data")
        with_device_token = await other.delete(
            f"{API}/fitness/data", headers={"Authorization": f"Bearer {device}"}
        )
    assert (anonymous.status_code, with_device_token.status_code) == (401, 401)
    assert (await client.get(f"{API}/fitness/daily")).json()["steps"] == 4000


async def test_deleting_with_nothing_stored_is_harmless(
    client: httpx.AsyncClient, device: str
) -> None:
    response = await client.delete(f"{API}/fitness/data")
    assert response.json() == {"days": 0, "workouts": 0, "sync_batches": 0}
