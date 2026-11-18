"""Fitness read endpoints, sync status, and clearly-labelled test activity."""

import uuid
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
    await seed_configuration(session_factory)
    await register_and_login(client)
    return await pair_device(client)


# ------------------------------------------------------------------------ views


async def test_daily_view_shows_activity_and_what_it_earned(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(
        client,
        device,
        days=[
            day(
                TODAY,
                8000,
                distance_walking_running_m="6100.5",
                distance_cycling_m="12000",
                active_energy_kcal="410.25",
            )
        ],
        workouts=[
            workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 30, distance_m="12000"),
        ],
    )

    view = (await client.get(f"{API}/fitness/daily")).json()

    assert view == {
        "date": TODAY,
        "source": "apple_healthkit",
        "is_test_data": False,
        "steps": 8000,
        "walking_distance_m": "6100.50",
        "running_distance_m": "0.00",
        "cycling_distance_m": "12000.00",
        "active_energy_kcal": "410.25",
        "eligible_minutes": 30,
        "step_credits_wc": 800,
        "workout_credits_wc": 150,
        "entitled_wc": 950,
        "awarded_wc": 950,
        "capped": False,
        "synced_at": view["synced_at"],
    }
    assert view["synced_at"].startswith("2026-11-13T12:00:00")


async def test_a_day_without_data_is_zeroes_not_an_error(
    client: httpx.AsyncClient, device: str
) -> None:
    view = (await client.get(f"{API}/fitness/daily", params={"date": "2026-11-01"})).json()
    assert (view["date"], view["source"], view["steps"], view["awarded_wc"]) == (
        "2026-11-01",
        None,
        0,
        0,
    )
    assert (await client.get(f"{API}/fitness/daily", params={"date": "nope"})).status_code == 422


async def test_history_is_newest_first_and_bounded(client: httpx.AsyncClient, device: str) -> None:
    await sync(
        client,
        device,
        days=[day("2026-11-09", 3000), day("2026-11-11", 16000), day(TODAY, 500)],
    )

    default = (await client.get(f"{API}/fitness/history")).json()
    ranged = (
        await client.get(f"{API}/fitness/history", params={"from": "2026-11-10", "to": YESTERDAY})
    ).json()

    assert [(d["date"], d["steps"], d["awarded_wc"], d["capped"]) for d in default] == [
        (TODAY, 500, 50, False),
        ("2026-11-11", 16000, 1500, True),
        ("2026-11-09", 3000, 300, False),
    ]
    assert [d["date"] for d in ranged] == ["2026-11-11"]

    backwards = await client.get(
        f"{API}/fitness/history", params={"from": TODAY, "to": "2026-11-01"}
    )
    too_wide = await client.get(
        f"{API}/fitness/history", params={"from": "2024-01-01", "to": TODAY}
    )
    assert (backwards.status_code, too_wide.status_code) == (422, 422)


async def test_workouts_view_explains_why_each_does_or_does_not_earn(
    client: httpx.AsyncClient, device: str
) -> None:
    await sync(
        client,
        device,
        workouts=[
            workout(
                "EARNS", "cycling", f"{TODAY}T06:00:00+00:00", 30, origin_device="Xiaomi Watch S3"
            ),
            workout("RUN", "running", f"{TODAY}T07:00:00+00:00", 30),
            workout("TYPED", "cycling", f"{TODAY}T08:00:00+00:00", 30, was_user_entered=True),
            workout("UNKNOWN", "curling", f"{TODAY}T09:00:00+00:00", 30),
            workout("OTHER", "other", f"{TODAY}T10:00:00+00:00", 30),
            workout("EXCLUDED", "yoga", f"{TODAY}T10:45:00+00:00", 20),
            workout("GONE", "rowing", f"{TODAY}T11:15:00+00:00", 20),
        ],
    )  # fmt: skip
    listed = (await client.get(f"{API}/fitness/workouts")).json()
    excluded_id = next(w["id"] for w in listed if w["external_id"] == "EXCLUDED")
    await client.patch(
        f"{API}/fitness/workouts/{excluded_id}", json={"excluded_from_rewards": True}
    )
    await sync(client, device, deleted=["GONE"])

    listed = (
        await client.get(f"{API}/fitness/workouts", params={"include_deleted": "true"})
    ).json()

    assert [w["external_id"] for w in listed] == [
        "GONE", "EXCLUDED", "OTHER", "UNKNOWN", "TYPED", "RUN", "EARNS",
    ]  # fmt: skip
    assert {w["external_id"]: (w["category"], w["earns_minutes"], w["reason"]) for w in listed} == {
        "EARNS": ("non_step", True, "eligible"),
        "RUN": ("step_based", False, "step_based"),
        "TYPED": ("non_step", False, "user_entered"),
        "UNKNOWN": ("unclassified", False, "unclassified_type"),
        "OTHER": ("excluded", False, "type_excluded"),
        "EXCLUDED": ("non_step", False, "excluded"),
        "GONE": ("non_step", False, "deleted"),
    }
    earns = next(w for w in listed if w["external_id"] == "EARNS")
    assert earns["duration_minutes"] == "30.00"
    assert earns["origin_device"] == "Xiaomi Watch S3"
    assert earns["source"] == "apple_healthkit"
    # EARNS (150) plus the rowing session (100): it was paid before it was deleted in
    # Apple Health, and a deletion that arrives by sync does not claw credits back.
    # The yoga session's 100 were reversed by the explicit exclusion.
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 250


async def test_fitness_views_require_a_session_and_are_private(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as alice,
        make_client(app) as bob,
        make_client(app) as anonymous,
    ):
        await seed_configuration(app.state.session_factory)
        await register_and_login(alice, "alice@example.com")
        await register_and_login(bob, "bob@example.com")
        token = await pair_device(alice)
        await sync(
            alice,
            token,
            days=[day(TODAY, 9000)],
            workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 30)],
        )

        for path in ("/fitness/daily", "/fitness/history", "/fitness/workouts"):
            assert (await anonymous.get(f"{API}{path}")).status_code == 401
            # The device reports activity; it cannot read it back.
            with_device = await anonymous.get(
                f"{API}{path}", headers={"Authorization": f"Bearer {token}"}
            )
            assert with_device.status_code == 401
        assert (await bob.get(f"{API}/fitness/daily")).json()["steps"] == 0
        assert (await bob.get(f"{API}/fitness/history")).json() == []
        assert (await bob.get(f"{API}/fitness/workouts")).json() == []
        assert (await bob.get(f"{API}/wallet")).json()["available_wc"] == 0
        assert (await alice.get(f"{API}/fitness/daily")).json()["steps"] == 9000

        # Activity is credited to the device's own user, whoever else exists.
        bob_token = await pair_device(bob)
        await sync(bob, bob_token, days=[day(TODAY, 2000)])
        assert (await bob.get(f"{API}/wallet")).json()["available_wc"] == 200
        assert (await alice.get(f"{API}/wallet")).json()["available_wc"] == 1050


async def test_sync_status_for_the_web_app_and_for_the_device(
    client: httpx.AsyncClient, device: str, make_client: ClientFactory, clock: MutableClock
) -> None:
    before = (await client.get(f"{API}/fitness/sync/status")).json()
    assert (before["last_sync_at"], before["batches"]) == (None, 0)

    await sync(
        client,
        device,
        days=[day(TODAY, 100), day(YESTERDAY, 100)],
        workouts=[workout("C", "cycling", f"{TODAY}T07:00:00+00:00", 30)],
    )
    clock.advance(minutes=10)

    async with make_client() as phone:
        as_device = await phone.get(
            f"{API}/fitness/sync/status", headers={"Authorization": f"Bearer {device}"}
        )
        anonymous = await phone.get(f"{API}/fitness/sync/status")
    as_session = await client.get(f"{API}/fitness/sync/status")

    assert anonymous.status_code == 401
    assert as_device.status_code == as_session.status_code == 200
    assert as_device.json() == as_session.json()
    status = as_device.json()
    assert status == {
        "last_sync_at": status["last_sync_at"],
        "last_source": "apple_healthkit",
        "batches": 1,
        "days_stored": 2,
        "workouts_stored": 1,
        # What a device needs to bucket activity into the same days the server rewards:
        "timezone": "Europe/Lisbon",
        "reward_window_days": 7,
        "sync_window_days": 30,
    }
    assert status["last_sync_at"].startswith("2026-11-13T12:00:00")
    listed = (await client.get(f"{API}/auth/devices")).json()
    assert listed[0]["last_used_at"].startswith("2026-11-13T12:10:00")


# -------------------------------------------------------------------- test data


def sample_batch(**content: Any) -> dict[str, Any]:
    return {"batch_id": str(uuid.uuid4()), **content}


async def test_test_data_is_refused_unless_explicitly_enabled(
    client: httpx.AsyncClient, device: str
) -> None:
    response = await client.post(
        f"{API}/fitness/sync/test", json=sample_batch(days=[day(TODAY, 15000)])
    )
    assert response.status_code == 403
    assert response.json()["code"] == "test_data_disabled"
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 0


async def test_test_data_earns_credits_that_are_labelled_everywhere(
    make_app: AppFactory, make_client: ClientFactory, clock: MutableClock
) -> None:
    async with make_app(allow_test_fitness_data=True) as app, make_client(app) as admin:
        sessions: Sessions = app.state.session_factory
        await seed_configuration(sessions)
        await register_and_login(admin)

        response = await admin.post(
            f"{API}/fitness/sync/test",
            json=sample_batch(
                days=[day(TODAY, 8000)],
                workouts=[
                    workout("T-1", "traditional_strength_training", f"{TODAY}T07:00:00+00:00", 30)
                ],
            ),
        )

        assert response.status_code == 200
        body = response.json()
        assert (body["source"], body["is_test_data"]) == ("test", True)
        assert body["rewards"][0]["is_test_data"] is True
        assert body["rewards"][0]["awarded_now_wc"] == 950
        assert body["wallet"]["available_wc"] == 950

        entry = (await admin.get(f"{API}/wallet/history")).json()["items"][0]
        assert entry["description"] == f"[TEST DATA] Activity reward for {TODAY}"
        daily = (await admin.get(f"{API}/fitness/daily")).json()
        assert (daily["source"], daily["is_test_data"]) == ("test", True)
        listed = (await admin.get(f"{API}/fitness/workouts")).json()
        assert (listed[0]["source"], listed[0]["is_test_data"], listed[0]["reason"]) == (
            "test",
            True,
            "eligible",
        )
        async with sessions() as session:
            flags = (
                await session.execute(
                    text(
                        "SELECT (SELECT bool_and(is_test_data) FROM fitness_daily), "
                        "(SELECT bool_and(is_test_data) FROM workout_events), "
                        "(SELECT bool_and(is_test_data) FROM fitness_sync_batches), "
                        "(SELECT bool_and(is_test_data) FROM daily_rewards), "
                        "(SELECT bool_and((metadata->>'is_test_data')::boolean) "
                        " FROM wallet_transactions)"
                    )
                )
            ).one()
            audited = await session.scalar(
                text("SELECT count(*) FROM audit_log WHERE action = 'fitness.test_data_submitted'")
            )
        assert tuple(flags) == (True, True, True, True, True)
        assert audited == 1
        await assert_ledger_consistent(sessions, clock)


async def test_test_data_is_for_administrators_only(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(allow_test_fitness_data=True, registration_mode="open") as app,
        make_client(app) as admin,
        make_client(app) as user,
        make_client(app) as anonymous,
    ):
        await seed_configuration(app.state.session_factory)
        await register_and_login(admin, "admin@example.com")
        await register_and_login(user, "user@example.com")
        token = await pair_device(user)
        payload = sample_batch(days=[day(TODAY, 15000)])

        as_user = await user.post(f"{API}/fitness/sync/test", json=payload)
        as_device = await anonymous.post(
            f"{API}/fitness/sync/test", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        as_nobody = await anonymous.post(f"{API}/fitness/sync/test", json=payload)

        assert [r.status_code for r in (as_user, as_device, as_nobody)] == [403, 401, 401]
        assert (await user.get(f"{API}/wallet")).json()["available_wc"] == 0


async def test_real_activity_takes_precedence_over_test_data_for_the_same_day(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    """A day is paid from one source only: never test data on top of real data."""
    async with make_app(allow_test_fitness_data=True) as app, make_client(app) as admin:
        await seed_configuration(app.state.session_factory)
        await register_and_login(admin)
        token = await pair_device(admin)
        await admin.post(f"{API}/fitness/sync/test", json=sample_batch(days=[day(TODAY, 3000)]))
        assert (await admin.get(f"{API}/wallet")).json()["available_wc"] == 300

        real = await sync(admin, token, days=[day(TODAY, 5000)])

        reward = real["rewards"][0]
        assert (reward["source"], reward["is_test_data"], reward["steps"]) == (
            "apple_healthkit",
            False,
            5000,
        )
        assert reward["entitled_wc"] == 500  # 5,000 real steps — not 3,000 + 5,000
        assert reward["awarded_now_wc"] == 200
        daily = (await admin.get(f"{API}/fitness/daily")).json()
        assert (daily["source"], daily["steps"], daily["is_test_data"]) == (
            "apple_healthkit",
            5000,
            False,
        )


async def test_stored_test_data_stops_counting_once_the_switch_is_off(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    """Test rows left in a database cannot earn on an instance that forbids test data."""
    async with make_app(allow_test_fitness_data=True) as lenient, make_client(lenient) as admin:
        await seed_configuration(lenient.state.session_factory)
        account = await register_and_login(admin)
        await admin.post(
            f"{API}/fitness/sync/test",
            json=sample_batch(
                days=[day(YESTERDAY, 2000)],
                workouts=[workout("T", "cycling", f"{YESTERDAY}T07:00:00+00:00", 60)],
            ),
        )
        assert (await admin.get(f"{API}/wallet")).json()["available_wc"] == 500

        async with make_app(allow_test_fitness_data=False) as strict, make_client(strict) as http:
            for cookie in admin.cookies.jar:
                http.cookies.jar.set_cookie(cookie)
            recalculated = (
                await http.post(
                    f"{API}/admin/rewards/recalculate",
                    json={
                        "user_id": str(account.id),
                        "date_from": YESTERDAY,
                        "date_to": YESTERDAY,
                    },
                )
            ).json()
            listed = (await http.get(f"{API}/fitness/workouts")).json()

    assert (recalculated[0]["source"], recalculated[0]["entitled_wc"]) == (None, 0)
    assert recalculated[0]["awarded_now_wc"] == 0
    assert listed[0]["reason"] == "source_not_rewardable"
