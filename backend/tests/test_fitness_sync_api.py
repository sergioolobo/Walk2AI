"""Fitness synchronisation end to end: a paired device posts activity, the server
validates it and the reward engine pays — correctly and exactly once.

The clock starts at 2026-11-13 12:00 UTC, which is also 12:00 in Lisbon (winter time).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
YESTERDAY = "2026-11-12"


@pytest.fixture
async def device(client: httpx.AsyncClient, session_factory: Sessions) -> str:
    """A signed-in owner with the initial reward rules and one paired device."""
    await seed_configuration(session_factory)
    await register_and_login(client)
    return await pair_device(client)


async def balance(client: httpx.AsyncClient) -> int:
    available: int = (await client.get(f"{API}/wallet")).json()["available_wc"]
    return available


async def ledger(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = (await client.get(f"{API}/wallet/history")).json()["items"]
    return items


def reward_for(body: dict[str, Any], date_: str) -> dict[str, Any]:
    found: dict[str, Any] = next(r for r in body["rewards"] if r["date"] == date_)
    return found


# ------------------------------------------------------------------- the basics


async def test_specification_example_8000_steps_and_30_minutes_strength(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """8,000 steps = 800 WC, 30 minutes of strength training = 150 WC: 950 WC."""
    body = await sync(
        client,
        device,
        days=[day(YESTERDAY, 8000, distance_walking_running_m="6120.40")],
        workouts=[
            workout("HK-STRENGTH-1", "traditionalStrengthTraining", "2026-11-12T18:00:00+00:00", 30)
        ],
    )

    assert body["replayed"] is False
    assert body["source"] == "apple_healthkit"
    assert body["is_test_data"] is False
    assert body["days"] == [{**body["days"][0], "date": YESTERDAY, "status": "accepted"}]
    assert body["workouts"][0]["category"] == "non_step"
    assert body["workouts"][0]["earns_minutes"] is True
    reward = reward_for(body, YESTERDAY)
    assert (reward["steps"], reward["eligible_minutes"]) == (8000, 30)
    assert (reward["step_credits_wc"], reward["workout_credits_wc"]) == (800, 150)
    assert (reward["entitled_wc"], reward["awarded_now_wc"], reward["awarded_total_wc"]) == (
        950,
        950,
        950,
    )
    assert reward["capped"] is False
    assert body["wallet"] == {"available_wc": 950, "reserved_wc": 0}

    wallet = (await client.get(f"{API}/wallet")).json()
    assert (wallet["available_wc"], wallet["lifetime_earned_wc"]) == (950, 950)
    assert wallet["available_eur"] == "0.0950"  # 950 WC = EUR 0.095

    entries = await ledger(client)
    assert len(entries) == 1
    assert entries[0]["type"] == "ACTIVITY_REWARD"
    assert entries[0]["amount_wc"] == 950
    assert entries[0]["description"] == f"Activity reward for {YESTERDAY}"
    assert entries[0]["reference_type"] == "daily_reward"
    await assert_ledger_consistent(session_factory, clock)


@pytest.mark.parametrize(
    ("steps", "credits"),
    [(1000, 100), (5000, 500), (10000, 1000), (999, 99), (0, 0)],
)
async def test_reward_table_for_steps(
    client: httpx.AsyncClient, device: str, steps: int, credits: int
) -> None:
    body = await sync(client, device, days=[day(TODAY, steps)])
    assert reward_for(body, TODAY)["entitled_wc"] == credits
    assert await balance(client) == credits


@pytest.mark.parametrize("kind", ["traditional_strength_training", "cycling", "Cycling"])
async def test_thirty_minutes_of_strength_or_cycling_earns_150(
    client: httpx.AsyncClient, device: str, kind: str
) -> None:
    body = await sync(
        client, device, workouts=[workout("HK-1", kind, "2026-11-13T07:00:00+00:00", 30)]
    )
    assert reward_for(body, TODAY)["workout_credits_wc"] == 150
    assert await balance(client) == 150


async def test_the_payload_carries_facts_only(client: httpx.AsyncClient, device: str) -> None:
    """A device cannot state, suggest or influence what its activity is worth."""
    for smuggled in (
        {"days": [{"date": TODAY, "steps": 100, "credits": 99999}]},
        {"days": [{"date": TODAY, "steps": 100}], "awarded_wc": 99999},
        {"days": [{"date": TODAY, "steps": 100, "entitled_wc": 99999}]},
        {"workouts": [{**workout("W", "cycling", "2026-11-13T07:00:00+00:00", 30), "wc": 5000}]},
        {"days": [{"date": TODAY, "steps": 100}], "source": "test"},
    ):
        response = await client.post(
            f"{API}/fitness/sync",
            headers={"Authorization": f"Bearer {device}"},
            json={"batch_id": str(uuid.uuid4()), **smuggled},
        )
        assert response.status_code == 422, smuggled
    assert await balance(client) == 0


async def test_sync_requires_a_valid_device_token(
    client: httpx.AsyncClient, device: str, make_client: ClientFactory
) -> None:
    payload = {"batch_id": str(uuid.uuid4()), "days": [day(TODAY, 5000)]}
    async with make_client() as anonymous:
        no_credentials = await anonymous.post(f"{API}/fitness/sync", json=payload)
        wrong_token = await anonymous.post(
            f"{API}/fitness/sync", json=payload, headers={"Authorization": "Bearer w2a_dev_nope"}
        )
    # A browser session is not a device: the web app cannot post activity either.
    session_only = await client.post(f"{API}/fitness/sync", json=payload)

    assert [r.status_code for r in (no_credentials, wrong_token, session_only)] == [401, 401, 401]
    assert await balance(client) == 0

    devices = (await client.get(f"{API}/auth/devices")).json()
    await client.delete(f"{API}/auth/devices/{devices[0]['id']}")
    await sync(client, device, days=[day(TODAY, 5000)], expect=401)  # revoked
    assert await balance(client) == 0


# ------------------------------------------------- never reward the same thing twice


async def test_replaying_a_batch_pays_nothing_and_returns_the_same_result(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    batch_id = str(uuid.uuid4())
    first = await sync(client, device, days=[day(TODAY, 8000)], batch_id=batch_id)
    replays = [
        await sync(client, device, days=[day(TODAY, 8000)], batch_id=batch_id) for _ in range(4)
    ]

    assert first["replayed"] is False
    for replay in replays:
        assert replay["replayed"] is True
        assert replay["rewards"] == first["rewards"]
        assert replay["wallet"] == {"available_wc": 800, "reserved_wc": 0}
    assert await balance(client) == 800
    assert len(await ledger(client)) == 1
    await assert_ledger_consistent(session_factory, clock)


async def test_a_batch_id_cannot_be_reused_for_different_data(
    client: httpx.AsyncClient, device: str
) -> None:
    batch_id = str(uuid.uuid4())
    await sync(client, device, days=[day(TODAY, 8000)], batch_id=batch_id)
    refused = await sync(client, device, days=[day(TODAY, 15000)], batch_id=batch_id, expect=409)
    assert refused["code"] == "batch_id_reused"
    assert await balance(client) == 800


async def test_the_same_day_sent_in_many_different_batches_is_paid_once(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """Duplicate step records: the rolling sync window re-sends recent days every time."""
    for _ in range(6):
        body = await sync(client, device, days=[day(YESTERDAY, 8000), day(TODAY, 3000)])
    assert reward_for(body, YESTERDAY)["awarded_now_wc"] == 0
    assert reward_for(body, YESTERDAY)["awarded_total_wc"] == 800
    assert await balance(client) == 1100
    assert len(await ledger(client)) == 2  # one entry per day, not per sync
    await assert_ledger_consistent(session_factory, clock)


async def test_a_growing_day_is_paid_only_the_difference(
    client: httpx.AsyncClient, device: str
) -> None:
    """Steps accumulate through the day; each sync tops the reward up."""
    paid = []
    for steps in (2000, 2000, 5500, 5500, 9000):
        body = await sync(client, device, days=[day(TODAY, steps)])
        paid.append(reward_for(body, TODAY)["awarded_now_wc"])

    assert paid == [200, 0, 350, 0, 350]
    assert await balance(client) == 900
    amounts = [entry["amount_wc"] for entry in await ledger(client)]
    assert sorted(amounts) == [200, 350, 350]


async def test_the_same_workout_resent_does_not_add_minutes(
    client: httpx.AsyncClient, device: str
) -> None:
    session = workout("HK-CYCLE-1", "cycling", "2026-11-13T07:00:00+00:00", 40)
    for _ in range(5):
        body = await sync(client, device, workouts=[session])
    assert reward_for(body, TODAY)["eligible_minutes"] == 40
    assert await balance(client) == 200


async def test_the_same_session_under_two_ids_is_one_workout(
    client: httpx.AsyncClient, device: str
) -> None:
    """Duplicate workouts: Mi Fitness and another app both write the same session."""
    body = await sync(
        client,
        device,
        workouts=[
            workout("MI-FITNESS-77", "cycling", "2026-11-13T07:00:00+00:00", 45),
            workout("STRAVA-9001", "cycling", "2026-11-13T07:00:40+00:00", 44),
        ],
    )

    flags = {w["external_id"]: (w["duplicate"], w["earns_minutes"]) for w in body["workouts"]}
    assert sorted(flags.values()) == [(False, True), (True, False)]
    assert reward_for(body, TODAY)["eligible_minutes"] == 45
    assert await balance(client) == 225

    listed = (await client.get(f"{API}/fitness/workouts")).json()
    assert sorted(w["reason"] for w in listed) == ["duplicate", "eligible"]


async def test_overlapping_workouts_never_count_a_minute_twice(
    client: httpx.AsyncClient, device: str
) -> None:
    """18:00-18:40 strength and 18:20-19:00 cycling overlap by 20 minutes: 60, not 80."""
    body = await sync(
        client,
        device,
        workouts=[
            workout("A", "traditional_strength_training", "2026-11-12T18:00:00+00:00", 40),
            workout("B", "cycling", "2026-11-12T18:20:00+00:00", 40),
        ],
    )
    assert reward_for(body, YESTERDAY)["eligible_minutes"] == 60
    assert await balance(client) == 300


# -------------------------------------------------------- walking and running


async def test_walking_and_running_are_paid_through_steps_only(
    client: httpx.AsyncClient, device: str
) -> None:
    """A 10 km run is already in the step count: its workout adds no minutes."""
    body = await sync(
        client,
        device,
        days=[day(YESTERDAY, 12000, distance_walking_running_m="11500")],
        workouts=[
            workout("RUN-1", "running", "2026-11-12T07:00:00+00:00", 55, distance_m="10000"),
            workout("WALK-1", "walking", "2026-11-12T18:00:00+00:00", 40, distance_m="1500"),
            workout("HIKE-1", "hiking", "2026-11-12T12:00:00+00:00", 30),
        ],
    )

    assert {w["category"] for w in body["workouts"]} == {"step_based"}
    assert not any(w["earns_minutes"] for w in body["workouts"])
    reward = reward_for(body, YESTERDAY)
    assert (reward["step_credits_wc"], reward["workout_credits_wc"]) == (1200, 0)
    assert reward["eligible_minutes"] == 0
    assert await balance(client) == 1200  # not 1,200 + 125 minutes' worth

    view = (await client.get(f"{API}/fitness/daily", params={"date": YESTERDAY})).json()
    # HealthKit reports one combined distance; running comes from the running workout.
    assert (view["running_distance_m"], view["walking_distance_m"]) == ("10000.00", "1500.00")
    reasons = {w["type"]: w["reason"] for w in (await client.get(f"{API}/fitness/workouts")).json()}
    assert reasons == {"running": "step_based", "walking": "step_based", "hiking": "step_based"}


# ------------------------------------------------------------- non-eligible activity


async def test_an_unrecognised_workout_type_is_stored_but_earns_nothing(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await sync(
        client,
        device,
        workouts=[workout("X-1", "underwaterBasketWeaving", "2026-11-13T07:00:00+00:00", 60)],
    )
    assert body["workouts"][0]["type"] == "underwater_basket_weaving"
    assert body["workouts"][0]["category"] == "unclassified"
    assert body["workouts"][0]["earns_minutes"] is False
    assert await balance(client) == 0

    listed = (await client.get(f"{API}/fitness/workouts")).json()
    assert listed[0]["reason"] == "unclassified_type"

    # Once an administrator classifies it, the same stored workout starts to count.
    classified = await client.put(
        f"{API}/admin/workout-types/underwaterBasketWeaving", json={"category": "non_step"}
    )
    assert classified.status_code == 200
    me = (await client.get(f"{API}/auth/me")).json()
    recalculated = await client.post(
        f"{API}/admin/rewards/recalculate",
        json={"user_id": me["id"], "date_from": TODAY, "date_to": TODAY},
    )
    assert recalculated.json()[0]["awarded_now_wc"] == 300
    assert await balance(client) == 300


async def test_hand_entered_workouts_are_not_verified_and_earn_nothing(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await sync(
        client,
        device,
        workouts=[
            workout("TYPED-IN", "cycling", "2026-11-13T07:00:00+00:00", 120, was_user_entered=True)
        ],
    )
    assert body["workouts"][0]["verified"] is False
    assert body["workouts"][0]["earns_minutes"] is False
    assert await balance(client) == 0
    assert (await client.get(f"{API}/fitness/workouts")).json()[0]["reason"] == "user_entered"


async def test_types_classified_as_excluded_earn_nothing(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await sync(
        client, device, workouts=[workout("O-1", "other", "2026-11-13T07:00:00+00:00", 45)]
    )
    assert body["workouts"][0]["category"] == "excluded"
    assert await balance(client) == 0


# ------------------------------------------------------------------- daily limit


async def test_the_daily_cap_is_1500(client: httpx.AsyncClient, device: str) -> None:
    body = await sync(
        client,
        device,
        days=[day(TODAY, 20000)],
        workouts=[workout("C-1", "cycling", "2026-11-13T07:00:00+00:00", 60)],
    )
    reward = reward_for(body, TODAY)
    assert (reward["step_credits_wc"], reward["workout_credits_wc"]) == (2000, 300)
    assert (reward["entitled_wc"], reward["capped"]) == (1500, True)
    assert await balance(client) == 1500

    # More activity the same day earns nothing more.
    more = await sync(client, device, days=[day(TODAY, 35000)])
    assert reward_for(more, TODAY)["awarded_now_wc"] == 0
    assert await balance(client) == 1500


async def test_the_cap_applies_to_each_day_separately(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(client, device, days=[day(YESTERDAY, 30000), day(TODAY, 30000)])
    assert await balance(client) == 3000


# -------------------------------------------------------- missing and late data


async def test_an_empty_sync_is_harmless(client: httpx.AsyncClient, device: str) -> None:
    body = await sync(client, device)
    assert (body["days"], body["workouts"], body["rewards"]) == ([], [], [])
    assert await balance(client) == 0


async def test_workouts_without_a_step_record_still_earn_their_minutes(
    client: httpx.AsyncClient, device: str
) -> None:
    """Missing data: the day's step total has not arrived yet."""
    body = await sync(
        client,
        device,
        workouts=[workout("S-1", "traditional_strength_training", "2026-11-13T07:00:00+00:00", 30)],
    )
    assert reward_for(body, TODAY)["steps"] == 0
    assert await balance(client) == 150

    later = await sync(client, device, days=[day(TODAY, 6000)])  # the steps arrive
    assert reward_for(later, TODAY)["awarded_now_wc"] == 600
    assert await balance(client) == 750


async def test_late_data_is_paid_under_its_own_days_cap(
    client: httpx.AsyncClient, device: str, clock: MutableClock
) -> None:
    """Delayed synchronisation: the watch delivers three days at once."""
    body = await sync(
        client,
        device,
        days=[day("2026-11-10", 18000), day("2026-11-11", 4000), day(YESTERDAY, 9000)],
    )
    totals = {r["date"]: r["awarded_now_wc"] for r in body["rewards"]}
    assert totals == {"2026-11-10": 1500, "2026-11-11": 400, YESTERDAY: 900}
    assert await balance(client) == 2800

    # A week later the same days arrive again with slightly more steps: still paid.
    clock.advance(days=3)
    again = await sync(client, device, days=[day("2026-11-11", 4500)])
    assert reward_for(again, "2026-11-11")["awarded_now_wc"] == 50


async def test_activity_older_than_the_reward_window_is_stored_but_not_paid(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await sync(client, device, days=[day("2026-11-05", 10000), day("2026-11-06", 10000)])

    by_date = {r["date"]: r for r in body["rewards"]}
    # 8 days old: outside the 7-day reward window. 7 days old: just inside.
    assert by_date["2026-11-05"]["in_reward_window"] is False
    assert by_date["2026-11-05"]["awarded_now_wc"] == 0
    assert by_date["2026-11-05"]["entitled_wc"] == 1000
    assert by_date["2026-11-06"]["awarded_now_wc"] == 1000
    assert await balance(client) == 1000
    stored = (
        await client.get(f"{API}/fitness/history", params={"from": "2026-11-05", "to": TODAY})
    ).json()
    assert {d["date"]: d["steps"] for d in stored} == {"2026-11-05": 10000, "2026-11-06": 10000}


async def test_a_day_that_was_missed_cannot_be_claimed_after_the_window(
    client: httpx.AsyncClient, device: str, clock: MutableClock
) -> None:
    clock.advance(days=9)  # it is now 22 November
    body = await sync(client, device, days=[day(TODAY, 12000)])
    assert reward_for(body, TODAY)["awarded_now_wc"] == 0
    assert reward_for(body, TODAY)["in_reward_window"] is False
    assert body["wallet"]["available_wc"] == 0


async def test_a_downward_correction_does_not_claw_back_and_is_not_paid_twice(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """HealthKit re-merges sources and the total shrinks, then grows again."""
    await sync(client, device, days=[day(TODAY, 9000)])
    lower = await sync(client, device, days=[day(TODAY, 8200)])
    regrown = await sync(client, device, days=[day(TODAY, 8900)])
    higher = await sync(client, device, days=[day(TODAY, 9400)])

    assert reward_for(lower, TODAY)["entitled_wc"] == 820
    assert reward_for(lower, TODAY)["awarded_total_wc"] == 900  # nothing taken back
    assert reward_for(regrown, TODAY)["awarded_now_wc"] == 0  # still below what was paid
    assert reward_for(higher, TODAY)["awarded_now_wc"] == 40  # only above the high-water mark
    assert await balance(client) == 940
    await assert_ledger_consistent(session_factory, clock)


async def test_a_workout_deleted_in_apple_health_stops_counting_without_clawback(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(
        client,
        device,
        workouts=[
            workout("ORIGINAL", "cycling", "2026-11-13T07:00:00+00:00", 40),
            workout("COPY", "cycling", "2026-11-13T07:00:30+00:00", 40),
        ],
    )
    assert await balance(client) == 200

    gone = await sync(client, device, deleted=["ORIGINAL", "NEVER-EXISTED"])
    assert gone["deleted_workouts"] == 1
    # The copy stands on its own again, so the session is still counted once.
    assert reward_for(gone, TODAY)["eligible_minutes"] == 40
    assert await balance(client) == 200

    all_gone = await sync(client, device, deleted=["COPY"])
    assert reward_for(all_gone, TODAY)["eligible_minutes"] == 0
    assert reward_for(all_gone, TODAY)["awarded_total_wc"] == 200  # high-water mark
    assert await balance(client) == 200
    visible = (await client.get(f"{API}/fitness/workouts")).json()
    with_deleted = (
        await client.get(f"{API}/fitness/workouts", params={"include_deleted": "true"})
    ).json()
    assert (len(visible), len(with_deleted)) == (0, 2)


# ------------------------------------------------------------------- validation


async def test_implausible_records_are_rejected_without_blocking_the_rest(
    client: httpx.AsyncClient, device: str, session_factory: Sessions
) -> None:
    body = await sync(
        client,
        device,
        days=[
            day("2026-11-14", 5000),  # tomorrow
            day("2026-10-01", 5000),  # 43 days ago
            day(YESTERDAY, 250000),  # a quarter of a million steps
            day("2026-11-11", 3000, distance_walking_running_m="900000"),  # 900 km on foot
            day(TODAY, 4000),  # fine
            day(TODAY, 9999),  # the same date twice
        ],
        workouts=[
            workout("FUTURE", "cycling", "2026-11-13T18:00:00+00:00", 30),
            workout("ENDLESS", "cycling", "2026-11-12T01:00:00+00:00", 14 * 60),
            workout("LYING", "cycling", "2026-11-12T10:00:00+00:00", 20, active_minutes=90),
            workout("ANCIENT", "cycling", "2026-09-01T10:00:00+00:00", 30),
            workout("BADTYPE", "!!!", "2026-11-12T10:00:00+00:00", 30),
            workout("GOOD", "cycling", "2026-11-12T19:00:00+00:00", 30),
            workout("GOOD", "cycling", "2026-11-12T19:00:00+00:00", 30),
        ],
    )

    days = [(d["date"], d["status"], d.get("code")) for d in body["days"]]
    assert days == [
        ("2026-11-14", "rejected", "date_in_future"),
        ("2026-10-01", "rejected", "too_old"),
        (YESTERDAY, "rejected", "implausible_steps"),
        ("2026-11-11", "rejected", "implausible_distance"),
        (TODAY, "accepted", None),
        (TODAY, "rejected", "duplicate_in_batch"),
    ]
    workouts = {
        w["external_id"]: w.get("code") for w in body["workouts"] if w["external_id"] != "GOOD"
    }
    assert workouts == {
        "FUTURE": "in_future",
        "ENDLESS": "too_long",
        "LYING": "duration_exceeds_interval",
        "ANCIENT": "too_old",
        "BADTYPE": "invalid_type",
    }
    assert [w.get("code") for w in body["workouts"] if w["external_id"] == "GOOD"] == [
        None,
        "duplicate_in_batch",
    ]
    # Only the acceptable records were stored and rewarded.
    assert await balance(client) == 400 + 150
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM fitness_daily")) == 1
        assert await session.scalar(text("SELECT count(*) FROM workout_events")) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"days": [{"date": TODAY, "steps": -5}]},
        {"days": [{"date": TODAY, "steps": 1.5}]},
        {"days": [{"date": "13/11/2026", "steps": 100}]},
        {"days": [{"date": TODAY}]},
        {"workouts": [{"external_id": "W", "type": "cycling", "start": "2026-11-13T07:00:00",
                       "end": "2026-11-13T07:30:00", "duration_seconds": 1800}]},  # no offset
        {"workouts": [{"external_id": "W", "type": "cycling"}]},
        {"batch_id": "not-a-uuid"},
    ],
)  # fmt: skip
async def test_malformed_payloads_are_rejected_outright(
    client: httpx.AsyncClient, device: str, payload: dict[str, Any]
) -> None:
    response = await client.post(
        f"{API}/fitness/sync",
        headers={"Authorization": f"Bearer {device}"},
        json={"batch_id": str(uuid.uuid4()), **payload},
    )
    assert response.status_code == 422
    assert await balance(client) == 0


async def test_batch_size_is_bounded(make_app: AppFactory, make_client: ClientFactory) -> None:
    async with (
        make_app(max_days_per_sync=3, max_workouts_per_sync=2) as app,
        make_client(app) as http,
    ):
        await seed_configuration(app.state.session_factory)
        await register_and_login(http)
        token = await pair_device(http)
        too_many_days = await sync(
            http, token, days=[day(f"2026-11-{d:02d}", 100) for d in (10, 11, 12, 13)], expect=422
        )
        too_many_workouts = await sync(
            http,
            token,
            workouts=[
                workout(f"W{i}", "cycling", f"2026-11-13T0{i}:00:00+00:00", 10) for i in range(3)
            ],
            expect=422,
        )
    assert "at most 3 days" in too_many_days["detail"]
    assert "at most 2 workouts" in too_many_workouts["detail"]


# ------------------------------------------------------- Lisbon days, not UTC days


async def test_the_day_rolls_over_at_lisbon_midnight_in_summer(
    client: httpx.AsyncClient, device: str, clock: MutableClock
) -> None:
    """At 23:30 UTC on 14 July it is already 00:30 on the 15th in Lisbon (UTC+1)."""
    clock.set(datetime(2026, 7, 14, 23, 30, tzinfo=UTC))
    body = await sync(client, device, days=[day("2026-07-15", 1200), day("2026-07-16", 500)])

    statuses = {d["date"]: (d["status"], d.get("code")) for d in body["days"]}
    assert statuses == {
        "2026-07-15": ("accepted", None),  # "tomorrow" in UTC, but today in Lisbon
        "2026-07-16": ("rejected", "date_in_future"),
    }
    today = (await client.get(f"{API}/fitness/daily")).json()
    assert (today["date"], today["steps"], today["awarded_wc"]) == ("2026-07-15", 1200, 120)


async def test_a_workout_across_lisbon_midnight_is_split_between_the_two_days(
    client: httpx.AsyncClient, device: str, clock: MutableClock
) -> None:
    """23:30-00:30 local time on a summer night: 22:30-23:30 UTC, one UTC day."""
    clock.set(datetime(2026, 7, 15, 9, 0, tzinfo=UTC))
    body = await sync(
        client, device, workouts=[workout("LATE", "cycling", "2026-07-14T22:30:00+00:00", 60)]
    )
    minutes = {r["date"]: r["eligible_minutes"] for r in body["rewards"]}
    assert minutes == {"2026-07-14": 30, "2026-07-15": 30}
    assert await balance(client) == 300


async def test_daylight_saving_days_are_whole_days(
    client: httpx.AsyncClient, device: str, clock: MutableClock
) -> None:
    """25 October 2026 has 25 hours in Lisbon; the cap still applies once to it."""
    clock.set(datetime(2026, 10, 26, 8, 0, tzinfo=UTC))
    body = await sync(
        client,
        device,
        days=[day("2026-10-25", 6000)],
        workouts=[
            # 00:30 WEST (23:30 UTC on the 24th) and 23:30 WET (23:30 UTC on the 25th):
            # 24 hours apart on the UTC clock, both inside the same 25-hour local day.
            workout("EARLY", "cycling", "2026-10-24T23:30:00+00:00", 20),
            workout("LATE", "cycling", "2026-10-25T23:30:00+00:00", 20),
        ],
    )
    reward = reward_for(body, "2026-10-25")
    assert reward["eligible_minutes"] == 40
    assert reward["entitled_wc"] == 600 + 200
    assert [r["date"] for r in body["rewards"]] == ["2026-10-25"]
