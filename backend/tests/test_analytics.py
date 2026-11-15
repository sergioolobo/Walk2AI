"""Dashboard analytics: grouped by the user's own calendar, with no gaps, and always
agreeing with the records they summarise (fitness history and the ledger)."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import TransactionType
from app.services.ai.pricing import TokenUsage
from app.services.ai.providers import FailureCode
from app.services.wallet import WalletService
from tests.ai_support import (
    HAIKU,
    Fail,
    FakeProviders,
    chat_events,
    prepare_catalogue,
    reply,
    second_user,
    send_chat,
)
from tests.support import (
    API,
    MutableClock,
    day,
    login,
    pair_device,
    register_and_login,
    sync,
    workout,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

USAGE = TokenUsage(input_tokens=1_000, output_tokens=300)  # 3 WC on Haiku


async def report(client: httpx.AsyncClient, kind: str, **params: str) -> dict[str, Any]:
    response = await client.get(f"{API}/analytics/{kind}", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
async def device(client: httpx.AsyncClient, session_factory: Sessions) -> str:
    await prepare_catalogue(session_factory)
    await register_and_login(client)
    token = await pair_device(client)
    await sync(
        client,
        token,
        days=[
            day("2026-11-09", 3_000, distance_walking_running_m="2100.5"),
            day("2026-11-11", 16_000, distance_walking_running_m="11000"),
            day("2026-11-13", 8_000, distance_walking_running_m="6000"),
        ],
        workouts=[workout("C", "cycling", "2026-11-13T07:00:00+00:00", 30, distance_m="12000")],
    )
    return token


async def test_activity_by_day_covers_every_day_and_matches_the_fitness_history(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await report(client, "activity", **{"from": "2026-11-07", "to": "2026-11-13"})
    history = (
        await client.get(
            f"{API}/fitness/history", params={"from": "2026-11-07", "to": "2026-11-13"}
        )
    ).json()

    assert body["period"] == {
        "timezone": "Europe/Lisbon",
        "granularity": "day",
        "from": "2026-11-07",
        "to": "2026-11-13",
    }
    points = {p["from"]: p for p in body["points"]}
    assert list(points) == [f"2026-11-{d:02d}" for d in range(7, 14)]  # empty days included
    assert all(p["from"] == p["to"] for p in body["points"])
    assert [points[d]["steps"] for d in points] == [0, 0, 3_000, 0, 16_000, 0, 8_000]
    assert points["2026-11-13"]["eligible_minutes"] == 30  # the ride; walking pays by steps
    assert points["2026-11-09"]["distance_m"] == "2100.50"
    assert body["total_steps"] == sum(d["steps"] for d in history) == 27_000
    assert body["total_distance_m"] == "19100.50"


async def test_activity_rolls_up_into_monday_weeks_and_calendar_months(
    client: httpx.AsyncClient, device: str
) -> None:
    weeks = await report(
        client, "activity", granularity="week", **{"from": "2026-11-01", "to": "2026-11-13"}
    )
    months = await report(
        client, "activity", granularity="month", **{"from": "2026-10-15", "to": "2026-11-13"}
    )

    # 2026-11-01 is a Sunday: the first week is clipped to the range.
    assert [(p["from"], p["to"], p["steps"]) for p in weeks["points"]] == [
        ("2026-11-01", "2026-11-01", 0),
        ("2026-11-02", "2026-11-08", 0),
        ("2026-11-09", "2026-11-13", 27_000),
    ]
    assert weeks["points"][-1]["active_days"] == 3
    assert [(p["from"], p["to"], p["steps"]) for p in months["points"]] == [
        ("2026-10-15", "2026-10-31", 0),
        ("2026-11-01", "2026-11-13", 27_000),
    ]


async def test_rewards_show_what_was_paid_and_where_the_cap_applied(
    client: httpx.AsyncClient, device: str
) -> None:
    body = await report(client, "rewards", **{"from": "2026-11-09", "to": "2026-11-13"})
    wallet = (await client.get(f"{API}/wallet")).json()

    points = {p["from"]: p for p in body["points"]}
    assert (points["2026-11-09"]["step_wc"], points["2026-11-09"]["earned_wc"]) == (300, 300)
    # 16,000 steps are worth 1,600 WC; the daily cap pays 1,500.
    assert (points["2026-11-11"]["step_wc"], points["2026-11-11"]["earned_wc"]) == (1_600, 1_500)
    assert points["2026-11-11"]["capped_days"] == 1
    assert (points["2026-11-13"]["step_wc"], points["2026-11-13"]["workout_wc"]) == (800, 150)
    assert body["total_earned_wc"] == wallet["lifetime_earned_wc"] == 300 + 1_500 + 950


async def test_ai_usage_is_grouped_by_the_lisbon_day_through_summer_time(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
) -> None:
    """23:30 UTC on 14 July is 00:30 on 15 July in Lisbon (UTC+1); 23:30 UTC on
    13 November is still 13 November (UTC+0)."""
    ids = await prepare_catalogue(session_factory)
    clock.set(datetime(2026, 7, 14, 23, 30, tzinfo=UTC))
    account = await register_and_login(client)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).adjust(
            account.id, 1_000, kind=TransactionType.ADMIN_ADJUSTMENT, reason="t", actor_id=None
        )
    fake_providers.anthropic.script(*reply("Verão.", usage=USAGE))
    fake_providers.anthropic.script(*reply("Inverno.", usage=USAGE))
    fake_providers.anthropic.script(Fail(FailureCode.UNAVAILABLE))
    await chat_events(await send_chat(client, ids[HAIKU]))

    clock.set(datetime(2026, 11, 13, 23, 30, tzinfo=UTC))
    await login(client, account)
    await chat_events(await send_chat(client, ids[HAIKU]))
    await chat_events(await send_chat(client, ids[HAIKU]))  # fails before any text: free

    summer = await report(client, "ai-usage", **{"from": "2026-07-14", "to": "2026-07-15"})
    winter = await report(client, "ai-usage", **{"from": "2026-11-13", "to": "2026-11-14"})
    year = await report(
        client, "ai-usage", granularity="month", **{"from": "2026-07-01", "to": "2026-11-30"}
    )

    assert [(p["from"], p["requests"]) for p in summer["points"]] == [
        ("2026-07-14", 0),
        ("2026-07-15", 1),
    ]
    assert [(p["from"], p["requests"], p["failed"]) for p in winter["points"]] == [
        ("2026-11-13", 2, 1),
        ("2026-11-14", 0, 0),
    ]
    assert [p["credits_wc"] for p in year["points"]] == [3, 0, 0, 0, 3]
    assert year["totals"] == {
        "requests": 3,
        "failed": 1,
        "input_tokens": 2_000,
        "cached_tokens": 0,
        "output_tokens": 600,
        "reasoning_tokens": 0,
        "cost_eur": "0.00046000",
        "credits_wc": 6,
        "cost_eur_per_request": "0.00015333",  # the failed request cost nothing
        "cost_eur_per_1k_tokens": "0.00017692",  # 0.00046 x 1,000 / 2,600
    }
    assert [(m["provider"], m["model"], m["requests"]) for m in year["by_model"]] == [
        ("anthropic", HAIKU, 3)
    ]


async def test_spending_follows_the_ledger(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
    device: str,
) -> None:
    ids = await prepare_catalogue(session_factory)
    fake_providers.anthropic.script(*reply("Uma.", usage=USAGE))
    fake_providers.anthropic.script(*reply("Duas.", usage=USAGE))
    await chat_events(await send_chat(client, ids[HAIKU]))
    await chat_events(await send_chat(client, ids[HAIKU]))

    body = await report(client, "spending", **{"from": "2026-11-12", "to": "2026-11-13"})

    # Rewards for every synced day were paid today, when the sync arrived.
    assert [(p["from"], p["earned_wc"], p["spent_wc"], p["net_wc"]) for p in body["points"]] == [
        ("2026-11-12", 0, 0, 0),
        ("2026-11-13", 2_750, 6, 2_744),
    ]
    assert (body["total_earned_wc"], body["total_spent_wc"]) == (2_750, 6)
    assert body["summary"]["today"]["spent_wc"] == 6


async def test_the_dashboard_brings_the_home_screen_together(
    client: httpx.AsyncClient, session_factory: Sessions, device: str
) -> None:
    body = await report(client, "dashboard")

    assert body["wallet"]["available_wc"] == 2_750
    assert body["today"]["from"] == "2026-11-13" and body["today"]["steps"] == 8_000
    assert [p["from"] for p in body["last_7_days"]] == [f"2026-11-{d:02d}" for d in range(7, 14)]
    assert body["ai_this_month"]["requests"] == 0
    assert body["summary"]["timezone"] == "Europe/Lisbon"


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-11-13", "to": "2026-11-01"},
        {"from": "2024-01-01", "to": "2026-11-13"},
        {"granularity": "year"},
        {"from": "13/11/2026"},
    ],
)
async def test_malformed_ranges_are_refused(
    client: httpx.AsyncClient, device: str, params: dict[str, str]
) -> None:
    for kind in ("activity", "rewards", "ai-usage", "spending"):
        response = await client.get(f"{API}/analytics/{kind}", params=params)
        assert response.status_code == 422, (kind, response.text)


async def test_analytics_require_a_signed_in_user(client: httpx.AsyncClient) -> None:
    for kind in ("activity", "rewards", "ai-usage", "spending", "dashboard"):
        assert (await client.get(f"{API}/analytics/{kind}")).status_code == 401


async def test_another_users_records_are_never_counted(
    client: httpx.AsyncClient, make_client: Any, fresh_app: Any, device: str
) -> None:
    async with make_client() as other:
        await second_user(fresh_app, other)
        body = await report(other, "activity", **{"from": "2026-11-09", "to": "2026-11-13"})
        spending = await report(other, "spending", **{"from": "2026-11-09", "to": "2026-11-13"})

    assert sum(p["steps"] for p in body["points"]) == 0
    assert spending["total_earned_wc"] == 0
