"""Reward rules: administration, and the guarantee that changing a rate never alters
what a day that has already begun is worth."""

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

# The clock starts on 2026-11-13 (UTC). A rule may start on 2026-11-15 at the earliest:
# the first date that has not begun anywhere on Earth.
EARLIEST = "2026-11-15"


@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: Sessions) -> httpx.AsyncClient:
    await seed_configuration(session_factory)
    await register_and_login(client)
    return client


async def rules(client: httpx.AsyncClient, kind: str | None = None) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = (await client.get(f"{API}/admin/reward-rules")).json()
    return [r for r in listed if kind is None or r["activity_type"] == kind]


def reward_for(body: dict[str, Any], date_: str) -> dict[str, Any]:
    found: dict[str, Any] = next(r for r in body["rewards"] if r["date"] == date_)
    return found


# ------------------------------------------------------------------- listing


async def test_initial_rules_match_the_specification(admin: httpx.AsyncClient) -> None:
    listed = {r["activity_type"]: r for r in await rules(admin)}

    assert set(listed) == {"steps", "workout_minutes", "daily_total"}
    assert listed["steps"]["conversion_rate"] == "0.100000"  # 1,000 steps = 100 WC
    assert listed["workout_minutes"]["conversion_rate"] == "5.000000"  # 30 minutes = 150 WC
    assert listed["daily_total"]["daily_limit"] == 1500
    assert listed["daily_total"]["conversion_rate"] is None
    assert {r["state"] for r in listed.values()} == {"in_force"}
    assert {r["effective_until"] for r in listed.values()} == {None}


async def test_reward_rule_routes_require_an_administrator(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as admin,
        make_client(app) as user,
        make_client(app) as anonymous,
    ):
        await seed_configuration(app.state.session_factory)
        await register_and_login(admin, "admin@example.com")
        await register_and_login(user, "user@example.com")
        rule_id = (await rules(admin, "steps"))[0]["id"]
        new_rule = {"activity_type": "steps", "conversion_rate": "100", "effective_from": EARLIEST}

        for http, expected in ((user, 403), (anonymous, 401)):
            responses = [
                await http.get(f"{API}/admin/reward-rules"),
                await http.post(f"{API}/admin/reward-rules", json=new_rule),
                await http.put(
                    f"{API}/admin/reward-rules/{rule_id}", json={"conversion_rate": "100"}
                ),
                await http.delete(f"{API}/admin/reward-rules/{rule_id}"),
                await http.get(f"{API}/admin/workout-types"),
                await http.put(f"{API}/admin/workout-types/cycling", json={"category": "excluded"}),
                await http.post(
                    f"{API}/admin/rewards/recalculate",
                    json={"user_id": rule_id, "date_from": EARLIEST, "date_to": EARLIEST},
                ),
            ]
            assert [r.status_code for r in responses] == [expected] * 7
        assert len(await rules(admin)) == 3


# ----------------------------------------------------- changing a rate, safely


async def test_a_new_rate_closes_the_current_rule_and_is_audited(
    admin: httpx.AsyncClient, session_factory: Sessions
) -> None:
    response = await admin.post(
        f"{API}/admin/reward-rules",
        json={"activity_type": "steps", "conversion_rate": "0.2", "effective_from": "2026-12-01"},
    )

    assert response.status_code == 201
    created = response.json()
    assert (created["state"], created["effective_from"], created["effective_until"]) == (
        "scheduled",
        "2026-12-01",
        None,
    )
    versions = await rules(admin, "steps")
    assert [
        (v["conversion_rate"], v["effective_from"], v["effective_until"], v["state"])
        for v in versions
    ] == [
        ("0.200000", "2026-12-01", None, "scheduled"),
        ("0.100000", "2026-01-01", "2026-12-01", "in_force"),
    ]
    async with session_factory() as session:
        details = await session.scalar(
            text("SELECT details FROM audit_log WHERE action = 'reward_rule.create'")
        )
    assert details == {
        "activity_type": "steps",
        "conversion_rate": "0.200000",
        "daily_limit": None,
        "effective_from": "2026-12-01",
        "effective_until": None,
    }


@pytest.mark.parametrize("start", ["2026-01-01", "2026-11-12", "2026-11-13", "2026-11-14"])
async def test_a_rule_cannot_start_on_a_day_that_has_already_begun_anywhere(
    admin: httpx.AsyncClient, start: str
) -> None:
    """Yesterday, today and even 'tomorrow' (already today in the Pacific) are refused."""
    response = await admin.post(
        f"{API}/admin/reward-rules",
        json={"activity_type": "steps", "conversion_rate": "5", "effective_from": start},
    )
    assert response.status_code == 422
    assert EARLIEST in response.json()["detail"]
    assert len(await rules(admin, "steps")) == 1


async def test_a_rate_change_never_alters_days_before_it_takes_effect(
    admin: httpx.AsyncClient, clock: MutableClock, session_factory: Sessions
) -> None:
    """Rule changes: activity dated before the change keeps the old rate even when it is
    synchronised afterwards; activity from the effective date gets the new one."""
    device = await pair_device(admin)
    before = await sync(admin, device, days=[day("2026-11-13", 4000)])
    assert reward_for(before, "2026-11-13")["awarded_now_wc"] == 400

    for body in (
        {"activity_type": "steps", "conversion_rate": "0.2", "effective_from": "2026-11-16"},
        {
            "activity_type": "workout_minutes",
            "conversion_rate": "10",
            "effective_from": "2026-11-16",
        },
        {"activity_type": "daily_total", "daily_limit": 3000, "effective_from": "2026-11-16"},
    ):
        assert (await admin.post(f"{API}/admin/reward-rules", json=body)).status_code == 201

    # The already-rewarded day is re-synchronised after the change was scheduled:
    again = await sync(admin, device, days=[day("2026-11-13", 4000)])
    assert reward_for(again, "2026-11-13")["awarded_now_wc"] == 0

    clock.set(datetime(2026, 11, 17, 9, 0, tzinfo=UTC))
    late = await sync(
        admin,
        device,
        days=[
            day("2026-11-13", 5000),  # grew since: paid the difference, at the OLD rate
            day("2026-11-15", 6000),  # last day of the old rules, arriving late
            day("2026-11-16", 6000),  # first day of the new rules
            day("2026-11-17", 20000),  # new rules, new cap
        ],
        workouts=[
            workout("OLD", "cycling", "2026-11-15T07:00:00+00:00", 30),
            workout("NEW", "cycling", "2026-11-16T07:00:00+00:00", 30),
        ],
    )

    by_date = {r["date"]: r for r in late["rewards"]}
    assert by_date["2026-11-13"]["awarded_now_wc"] == 100  # 5,000 x 0.1 = 500, 400 already paid
    assert by_date["2026-11-15"]["entitled_wc"] == 600 + 150  # 0.1 per step, 5 per minute
    assert by_date["2026-11-16"]["entitled_wc"] == 1200 + 300  # 0.2 per step, 10 per minute
    assert (by_date["2026-11-17"]["entitled_wc"], by_date["2026-11-17"]["capped"]) == (3000, True)
    await assert_ledger_consistent(session_factory, clock)

    async with session_factory() as session:
        snapshots: dict[str, str] = dict(
            (
                await session.execute(
                    text(
                        "SELECT activity_date::text, rules_snapshot->'steps'->>'rate' "
                        "FROM daily_rewards"
                    )
                )
            ).all()
        )
    # Each day records the rule it was paid under.
    assert snapshots["2026-11-15"] == "0.100000"
    assert snapshots["2026-11-16"] == "0.200000"


async def test_a_rule_in_force_can_only_be_given_an_end_date(admin: httpx.AsyncClient) -> None:
    rule = (await rules(admin, "steps"))[0]

    for change in ({"conversion_rate": "9"}, {"daily_limit": 50}, {"effective_from": "2027-01-01"}):
        refused = await admin.put(f"{API}/admin/reward-rules/{rule['id']}", json=change)
        assert refused.status_code == 409
        assert refused.json()["code"] == "rule_in_force"
    too_soon = await admin.put(
        f"{API}/admin/reward-rules/{rule['id']}", json={"effective_until": "2026-11-13"}
    )
    assert too_soon.status_code == 422

    ended = await admin.put(
        f"{API}/admin/reward-rules/{rule['id']}", json={"effective_until": "2026-12-01"}
    )
    assert ended.status_code == 200
    assert ended.json()["effective_until"] == "2026-12-01"
    assert ended.json()["conversion_rate"] == "0.100000"  # untouched
    reopened = await admin.put(
        f"{API}/admin/reward-rules/{rule['id']}", json={"effective_until": None}
    )
    assert reopened.json()["effective_until"] is None

    cannot_delete = await admin.delete(f"{API}/admin/reward-rules/{rule['id']}")
    assert cannot_delete.status_code == 409


async def test_a_scheduled_rule_can_be_edited_moved_and_withdrawn(
    admin: httpx.AsyncClient,
) -> None:
    created = (
        await admin.post(
            f"{API}/admin/reward-rules",
            json={
                "activity_type": "steps",
                "conversion_rate": "0.2",
                "effective_from": "2026-12-01",
            },
        )
    ).json()

    edited = await admin.put(
        f"{API}/admin/reward-rules/{created['id']}",
        json={"conversion_rate": "0.15", "daily_limit": 900, "effective_from": "2026-12-15"},
    )
    assert edited.status_code == 200
    versions = {v["effective_from"]: v for v in await rules(admin, "steps")}
    assert versions["2026-12-15"]["conversion_rate"] == "0.150000"
    assert versions["2026-12-15"]["daily_limit"] == 900
    # The rule it succeeds was moved along with it: no gap, no overlap.
    assert versions["2026-01-01"]["effective_until"] == "2026-12-15"

    into_the_past = await admin.put(
        f"{API}/admin/reward-rules/{created['id']}", json={"effective_from": "2026-11-13"}
    )
    assert into_the_past.status_code == 422

    withdrawn = await admin.delete(f"{API}/admin/reward-rules/{created['id']}")
    assert withdrawn.status_code == 204
    remaining = await rules(admin, "steps")
    assert [(r["effective_from"], r["effective_until"]) for r in remaining] == [
        ("2026-01-01", None)
    ]
    assert (await admin.delete(f"{API}/admin/reward-rules/{created['id']}")).status_code == 404


async def test_conflicting_or_malformed_rules_are_refused(admin: httpx.AsyncClient) -> None:
    scheduled = {"activity_type": "steps", "conversion_rate": "0.2", "effective_from": "2026-12-01"}
    assert (await admin.post(f"{API}/admin/reward-rules", json=scheduled)).status_code == 201

    earlier = await admin.post(
        f"{API}/admin/reward-rules", json={**scheduled, "effective_from": "2026-11-20"}
    )
    same_day = await admin.post(f"{API}/admin/reward-rules", json=scheduled)
    assert (earlier.status_code, same_day.status_code) == (409, 409)
    assert earlier.json()["code"] == "rule_conflict"

    for bad in (
        {"activity_type": "steps", "effective_from": EARLIEST},  # a rate is required
        {
            "activity_type": "daily_total",
            "conversion_rate": "1",
            "daily_limit": 5,
            "effective_from": EARLIEST,
        },
        {"activity_type": "daily_total", "effective_from": EARLIEST},  # a cap needs a limit
        {"activity_type": "steps", "conversion_rate": "-1", "effective_from": EARLIEST},
        {
            "activity_type": "steps",
            "conversion_rate": 0.1,
            "daily_limit": 1.5,
            "effective_from": EARLIEST,
        },
        {"activity_type": "naps", "conversion_rate": "1", "effective_from": EARLIEST},
        {
            "activity_type": "steps",
            "conversion_rate": "1",
            "effective_from": EARLIEST,
            "enabled": False,
        },
    ):
        assert (await admin.post(f"{API}/admin/reward-rules", json=bad)).status_code == 422, bad
    assert (await admin.put(f"{API}/admin/reward-rules/{scheduled}", json={})).status_code == 422
    assert len(await rules(admin)) == 4


async def test_per_activity_limits_cap_each_component(
    admin: httpx.AsyncClient, clock: MutableClock
) -> None:
    for body in (
        {
            "activity_type": "steps",
            "conversion_rate": "0.1",
            "daily_limit": 700,
            "effective_from": EARLIEST,
        },
        {
            "activity_type": "workout_minutes",
            "conversion_rate": "5",
            "daily_limit": 200,
            "effective_from": EARLIEST,
        },
    ):
        assert (await admin.post(f"{API}/admin/reward-rules", json=body)).status_code == 201
    device = await pair_device(admin)
    clock.set(datetime(2026, 11, 15, 20, 0, tzinfo=UTC))

    body = await sync(
        admin,
        device,
        days=[day("2026-11-15", 12000)],
        workouts=[workout("LONG", "cycling", "2026-11-15T07:00:00+00:00", 90)],
    )
    reward = reward_for(body, "2026-11-15")
    assert (reward["step_credits_wc"], reward["workout_credits_wc"]) == (700, 200)
    assert reward["entitled_wc"] == 900


async def test_without_rules_activity_earns_nothing(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    """No seed, no rules: rates are data, never constants hidden in the engine."""
    await register_and_login(client)
    device = await pair_device(client)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO workout_type_rules (workout_type, category) "
                "VALUES ('cycling', 'non_step')"
            )
        )
    body = await sync(
        client,
        device,
        days=[day("2026-11-13", 20000)],
        workouts=[workout("C", "cycling", "2026-11-13T07:00:00+00:00", 60)],
    )
    assert reward_for(body, "2026-11-13")["entitled_wc"] == 0
    assert body["wallet"]["available_wc"] == 0


# ----------------------------------------------------------------- workout types


async def test_workout_types_can_be_listed_and_reclassified(
    admin: httpx.AsyncClient, session_factory: Sessions
) -> None:
    listed = {
        t["workout_type"]: t["category"]
        for t in (await admin.get(f"{API}/admin/workout-types")).json()
    }
    assert listed["walking"] == listed["running"] == listed["hiking"] == "step_based"
    assert listed["cycling"] == listed["traditional_strength_training"] == "non_step"

    changed = await admin.put(f"{API}/admin/workout-types/yoga", json={"category": "excluded"})
    added = await admin.put(
        f"{API}/admin/workout-types/HKWorkoutActivityTypeBarre", json={"category": "non_step"}
    )
    invalid = await admin.put(f"{API}/admin/workout-types/yoga", json={"category": "bonus"})

    assert changed.json()["category"] == "excluded"
    assert added.json()["workout_type"] == "barre"  # HealthKit's spelling is normalised
    assert invalid.status_code == 422
    async with session_factory() as session:
        audited = (
            await session.execute(
                text(
                    "SELECT entity_id, details FROM audit_log "
                    "WHERE action = 'workout_type.classify' ORDER BY id"
                )
            )
        ).all()
    assert audited == [
        ("yoga", {"from": "non_step", "to": "excluded"}),
        ("barre", {"from": None, "to": "non_step"}),
    ]


async def test_reclassifying_a_type_does_not_claw_back_past_rewards(
    admin: httpx.AsyncClient,
) -> None:
    device = await pair_device(admin)
    await sync(admin, device, workouts=[workout("Y", "yoga", "2026-11-13T07:00:00+00:00", 40)])
    await admin.put(f"{API}/admin/workout-types/yoga", json={"category": "excluded"})

    me = (await admin.get(f"{API}/auth/me")).json()
    recalculated = (
        await admin.post(
            f"{API}/admin/rewards/recalculate",
            json={"user_id": me["id"], "date_from": "2026-11-13", "date_to": "2026-11-13"},
        )
    ).json()
    assert recalculated[0]["entitled_wc"] == 0
    assert recalculated[0]["awarded_total_wc"] == 200  # what was paid stays paid
    assert (await admin.get(f"{API}/wallet")).json()["available_wc"] == 200


async def test_recalculation_validates_its_range_and_user(admin: httpx.AsyncClient) -> None:
    me = (await admin.get(f"{API}/auth/me")).json()
    backwards = await admin.post(
        f"{API}/admin/rewards/recalculate",
        json={"user_id": me["id"], "date_from": "2026-11-13", "date_to": "2026-11-01"},
    )
    too_wide = await admin.post(
        f"{API}/admin/rewards/recalculate",
        json={"user_id": me["id"], "date_from": "2026-01-01", "date_to": "2026-11-13"},
    )
    unknown = await admin.post(
        f"{API}/admin/rewards/recalculate",
        json={
            "user_id": "00000000-0000-0000-0000-000000000000",
            "date_from": "2026-11-13",
            "date_to": "2026-11-13",
        },
    )
    assert (backwards.status_code, too_wide.status_code, unknown.status_code) == (422, 422, 404)
