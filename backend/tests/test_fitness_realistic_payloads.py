"""Activity shaped exactly as the iOS companion will send it.

The other fitness tests use tidy numbers. Real HealthKit data is not tidy: statistics
sums and durations are doubles (8423.0 steps, 1920.4532 seconds, 6120.38291204 metres),
Swift's ISO 8601 encoder writes UTC as ``Z`` and sometimes fractional seconds, workout
ids are uppercase UUIDs, type names are HealthKit's camelCase, and every sync re-sends a
rolling window of recent days. These tests drive the real device route with payloads of
that shape, and check that the test route runs exactly the same pipeline.

The clock starts at 2026-11-13 12:00 UTC (12:00 in Lisbon, winter time).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import AppFactory, ClientFactory
from tests.support import (
    API,
    MutableClock,
    assert_ledger_consistent,
    pair_device,
    register_and_login,
    seed_configuration,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

STRENGTH_UUID = "5E0B2C0A-6C1F-4F6B-9E57-2B1D0C7A9F10"
CYCLING_UUID = "9A1F3C44-0B7E-4D52-8E1A-6C2D8F0B3E21"
CYCLING_COPY_UUID = "C3D2E1F0-1A2B-4C3D-8E4F-5A6B7C8D9E0F"  # same ride, written by a second app
RUN_UUID = "1B2C3D4E-5F60-4718-89AB-CDEF01234567"
WALK_UUID = "7F6E5D4C-3B2A-4190-8F7E-6D5C4B3A2910"


def healthkit_payload(days: list[dict[str, Any]], workouts: list[dict[str, Any]]) -> dict[str, Any]:
    """A batch in the shape the companion's JSONEncoder produces."""
    return {
        "batch_id": str(uuid.uuid4()).upper(),  # Swift's UUID().uuidString is uppercase
        "device": {"timezone": "Europe/Lisbon", "app_version": "1.0.0 (1)"},
        "days": days,
        "workouts": workouts,
        "deleted_workout_ids": [],
    }


def hk_day(date_: str, steps: float, walking_running_m: float, energy: float) -> dict[str, Any]:
    return {
        "date": date_,
        "steps": steps,  # HKStatistics sums are Double
        "distance_walking_running_m": walking_running_m,
        "distance_cycling_m": 0.0,
        "active_energy_kcal": energy,
    }


def hk_workout(
    uuid_: str,
    kind: str,
    start: str,
    end: str,
    duration: float,
    *,
    distance: float | None = None,
    energy: float | None = None,
) -> dict[str, Any]:
    return {
        "external_id": uuid_,
        "type": kind,  # HealthKit's own spelling
        "start": start,
        "end": end,
        "duration_seconds": duration,  # HKWorkout.duration is a Double
        "distance_m": distance,
        "active_energy_kcal": energy,
        "origin_bundle_id": "example.mi-fitness",  # placeholder for the recording app
        "origin_device": "Xiaomi Watch S3",
        "was_user_entered": False,
    }


A_WEEK = healthkit_payload(
    days=[
        hk_day("2026-11-07", 6521.0, 4950.38291204, 301.5560000000001),
        hk_day("2026-11-08", 12873.0, 10118.0019, 655.12),
        hk_day("2026-11-09", 3104.0, 2390.5, 190.0),
        hk_day("2026-11-10", 9999.0, 7605.77777, 480.875),
        hk_day("2026-11-11", 15560.0, 12560.4, 840.3333),  # over the cap with steps alone
        hk_day("2026-11-12", 8423.0, 6120.38291204, 412.5560000000001),
        hk_day("2026-11-13", 4210.0, 3150.25, 205.0),  # today, still growing
    ],
    workouts=[
        # 29 min 59.9 s of strength training: rounds DOWN to 29 minutes, never up to 30.
        hk_workout(
            STRENGTH_UUID,
            "traditionalStrengthTraining",
            "2026-11-12T18:02:11Z",
            "2026-11-12T18:35:40Z",
            1799.9,
            energy=210.50817,
        ),
        # An evening ride; Swift sometimes writes fractional seconds.
        hk_workout(
            CYCLING_UUID,
            "cycling",
            "2026-11-10T17:30:00.250Z",
            "2026-11-10T18:15:00.750Z",
            2700.5,
            distance=18250.663,
            energy=402.1,
        ),
        # The same ride written to Apple Health a second time by another app.
        hk_workout(
            CYCLING_COPY_UUID,
            "HKWorkoutActivityTypeCycling",
            "2026-11-10T17:30:41Z",
            "2026-11-10T18:14:20Z",
            2619.0,
            distance=18100.0,
        ),
        # Running and walking: already in the step count.
        hk_workout(
            RUN_UUID,
            "running",
            "2026-11-08T07:05:00Z",
            "2026-11-08T07:52:30Z",
            2850.0,
            distance=8200.75,
        ),
        hk_workout(
            WALK_UUID,
            "walking",
            "2026-11-12T12:10:00Z",
            "2026-11-12T12:55:00Z",
            2700.0,
            distance=3400.0,
        ),
    ],
)

# What each day is worth: floor(0.1 x steps) + 5 x eligible minutes, capped at 1,500.
EXPECTED = {
    "2026-11-07": 652,  # six days old: still inside the 7-day reward window
    "2026-11-08": 1287,  # the run adds nothing on top of its steps
    "2026-11-09": 310,
    "2026-11-10": 999 + 45 * 5,  # one ride, counted once despite the second copy
    "2026-11-11": 1500,  # capped
    "2026-11-12": 842 + 29 * 5,  # strength: 29 whole minutes; the walk adds nothing
    "2026-11-13": 421,
}


async def post(client: httpx.AsyncClient, token: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        f"{API}/fitness/sync", headers={"Authorization": f"Bearer {token}"}, json=body
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.fixture
async def device(client: httpx.AsyncClient, session_factory: Sessions) -> str:
    await seed_configuration(session_factory)
    await register_and_login(client)
    return await pair_device(client)


async def test_a_realistic_week_from_healthkit_is_accepted_and_paid_exactly(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    body = await post(client, device, A_WEEK)

    assert {d["status"] for d in body["days"]} == {"accepted"}
    assert {w["status"] for w in body["workouts"]} == {"accepted"}
    paid = {r["date"]: r["awarded_now_wc"] for r in body["rewards"]}
    assert paid == EXPECTED
    assert body["wallet"]["available_wc"] == sum(EXPECTED.values())

    by_id = {w["external_id"]: w for w in body["workouts"]}
    assert by_id[STRENGTH_UUID]["type"] == "traditional_strength_training"
    assert by_id[CYCLING_COPY_UUID]["type"] == "cycling"  # HealthKit spelling normalised
    assert [by_id[i]["duplicate"] for i in (CYCLING_UUID, CYCLING_COPY_UUID)] == [False, True]
    assert by_id[RUN_UUID]["category"] == by_id[WALK_UUID]["category"] == "step_based"

    # Doubles were stored rounded, not rejected.
    day = (await client.get(f"{API}/fitness/daily", params={"date": "2026-11-12"})).json()
    assert (day["steps"], day["walking_distance_m"], day["active_energy_kcal"]) == (
        8423,
        "6120.38",
        "412.56",
    )
    assert day["eligible_minutes"] == 29
    listed = await client.get(f"{API}/fitness/workouts", params={"from": "2026-11-07"})
    workouts = {w["external_id"]: w for w in listed.json()}
    # 1,799.9 s -> 1,799 whole seconds -> 29.98 minutes (rounded down at every step)
    assert workouts[STRENGTH_UUID]["duration_minutes"] == "29.98"
    assert workouts[CYCLING_UUID]["distance_m"] == "18250.66"
    await assert_ledger_consistent(session_factory, clock)


async def test_the_rolling_window_resent_through_the_day_pays_only_what_is_new(
    client: httpx.AsyncClient, device: str, session_factory: Sessions, clock: MutableClock
) -> None:
    """The companion re-sends the last seven days on every sync. Through a day, only the
    growth of today's total — and genuinely late data — may earn anything."""
    await post(client, device, A_WEEK)
    first_total = sum(EXPECTED.values())

    totals = []
    for hours_later, today_steps, late_ride in (
        (1, 4210.0, False),  # nothing changed
        (3, 6888.0, False),  # walked more
        (5, 6888.0, True),  # the watch finally delivered this evening's ride
        (7, 9015.0, True),  # walked more again
    ):
        clock.set(datetime(2026, 11, 13, 12, tzinfo=UTC) + timedelta(hours=hours_later))
        days = [
            d if d["date"] != "2026-11-13" else {**d, "steps": today_steps} for d in A_WEEK["days"]
        ]
        workouts = list(A_WEEK["workouts"])
        if late_ride:
            workouts.append(
                hk_workout(
                    "0D9C8B7A-6F5E-4D3C-2B1A-098765432100",
                    "cycling",
                    "2026-11-13T15:00:00Z",
                    "2026-11-13T15:40:00Z",
                    2400.0,
                )
            )
        body = await post(client, device, healthkit_payload(days, workouts))
        totals.append(body["wallet"]["available_wc"])

    # Today: 4,210 -> 6,888 -> 9,015 steps, plus a 40-minute ride.
    assert totals == [
        first_total,
        first_total + (688 - 421),
        first_total + (688 - 421) + 200,
        first_total + (901 - 421) + 200,
    ]
    await client.post(f"{API}/auth/refresh")  # hours have passed: the web session refreshes
    ledger = (await client.get(f"{API}/wallet/history", params={"limit": 200})).json()["items"]
    # One entry per day the first time, then one per genuine increase. Not one per sync.
    assert len(ledger) == len(EXPECTED) + 3
    await assert_ledger_consistent(session_factory, clock)


async def test_test_activity_runs_exactly_the_same_pipeline_as_real_activity(
    make_app: AppFactory, make_client: ClientFactory, clock: MutableClock
) -> None:
    """The same payload through the device route and through the test route produces the
    same validation, de-duplication and rewards. Only the source label differs."""
    async with (
        make_app(allow_test_fitness_data=True, registration_mode="open") as app,
        make_client(app) as admin,
        make_client(app) as owner,
    ):
        sessions: Sessions = app.state.session_factory
        await seed_configuration(sessions)
        await register_and_login(admin, "admin@example.com")  # first account: administrator
        await register_and_login(owner, "owner@example.com")
        token = await pair_device(owner)

        real = await post(owner, token, A_WEEK)
        test_response = await admin.post(
            f"{API}/fitness/sync/test", json={**A_WEEK, "batch_id": str(uuid.uuid4())}
        )
        assert test_response.status_code == 200
        simulated = test_response.json()

        def without_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {k: v for k, v in row.items() if k not in ("source", "is_test_data")}
                for row in rows
            ]

        assert (real["source"], simulated["source"]) == ("apple_healthkit", "test")
        assert simulated["days"] == real["days"]
        assert simulated["workouts"] == real["workouts"]
        assert without_labels(simulated["rewards"]) == without_labels(real["rewards"])
        assert simulated["wallet"] == real["wallet"]

        real_ledger = (await owner.get(f"{API}/wallet/history", params={"limit": 200})).json()
        test_ledger = (await admin.get(f"{API}/wallet/history", params={"limit": 200})).json()
        assert [e["amount_wc"] for e in test_ledger["items"]] == [
            e["amount_wc"] for e in real_ledger["items"]
        ]
        assert all(e["description"].startswith("[TEST DATA]") for e in test_ledger["items"])
        assert not any("[TEST DATA]" in e["description"] for e in real_ledger["items"])
        await assert_ledger_consistent(sessions, clock)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steps", "NaN"),
        ("steps", -1.0),
        ("steps", True),
        ("distance_walking_running_m", -0.004),
        ("distance_walking_running_m", "Infinity"),
        ("active_energy_kcal", "a lot"),
    ],
)
async def test_values_that_are_not_finite_non_negative_numbers_are_still_rejected(
    client: httpx.AsyncClient, device: str, field: str, value: Any
) -> None:
    body = healthkit_payload([{**hk_day("2026-11-12", 8000.0, 6000.0, 400.0), field: value}], [])
    response = await client.post(
        f"{API}/fitness/sync", headers={"Authorization": f"Bearer {device}"}, json=body
    )
    assert response.status_code == 422
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 0
