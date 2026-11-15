"""The automatic USD→EUR rate from the ECB's daily reference rate.

No test contacts the ECB: its answer is served from an in-memory transport, or the rate
source is a plain function.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ai import ExchangeRate
from app.models.audit import AuditLog
from app.services.ai.reference_rates import (
    EcbRateFetcher,
    Outcome,
    ReferenceRate,
    ReferenceRateUnavailable,
    RefreshState,
    parse_ecb_csv,
    refresh_usd_eur_rate,
)
from tests.ai_support import (
    HAIKU,
    FakeProviders,
    chat_events,
    prepare_catalogue,
    reply,
    second_user,
    send_chat,
)
from tests.conftest import AppFactory, ClientFactory
from tests.support import API, MutableClock, register_and_login
from tests.test_ai_chat import USAGE, fund, usage_rows

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

# The ECB data API's real answer on 2026-11-15 (columns after UNIT_MULT omitted by none).
ECB_HEADER = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,"
    "OBS_CONF,OBS_PRE_BREAK,OBS_COM,TIME_FORMAT,BREAKS,COLLECTION,COMPILING_ORG,DISS_ORG,"
    "DOM_SER_IDS,PUBL_ECB,PUBL_MU,PUBL_PUBLIC,UNIT_INDEX_BASE,COMPILATION,COVERAGE,DECIMALS,"
    "NAT_TITLE,SOURCE_AGENCY,SOURCE_PUB,TITLE,TITLE_COMPL,UNIT,UNIT_MULT"
)


def ecb_csv(day: str = "2026-11-13", value: str = "1.1052", currency: str = "USD") -> str:
    return (
        f"{ECB_HEADER}\n"
        f"EXR.D.{currency}.EUR.SP00.A,D,{currency},EUR,SP00,A,{day},{value},A,F,,,P1D,,A,,,,,,,"
        '99Q1=100,,,4,,4F0,,US dollar/Euro ECB reference exchange rate,"ECB reference '
        'exchange rate, US dollar/Euro, 2.15 pm (C.E.T.)",USD,0\n'
    )


def mock_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def source_of(day: str, value: str) -> "RateSource":
    return RateSource(ReferenceRate(date.fromisoformat(day), Decimal(value)))


class RateSource:
    """A rate source the test controls."""

    def __init__(self, rate: ReferenceRate | None) -> None:
        self.rate = rate
        self.calls = 0

    async def __call__(self) -> ReferenceRate:
        self.calls += 1
        if self.rate is None:
            raise ReferenceRateUnavailable("The ECB could not be reached (ConnectError).")
        return self.rate


async def usd_rates(session_factory: Sessions) -> list[ExchangeRate]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(ExchangeRate)
            .where(ExchangeRate.base_currency == "USD")
            .order_by(ExchangeRate.effective_from)
        )
        return list(rows.all())


# ------------------------------------------------------------------- reading


def test_the_ecb_answer_is_read_and_inverted_rounding_up() -> None:
    rate = parse_ecb_csv(ecb_csv())

    assert (rate.published_for, rate.usd_per_eur) == (date(2026, 11, 13), Decimal("1.1052"))
    # 1 / 1.1052 = 0.904813608...; rounded up so a converted cost is never understated.
    assert rate.usd_to_eur == Decimal("0.90481361")
    assert rate.source == "ECB reference rate 2026-11-13: 1 EUR = 1.1052 USD"


@pytest.mark.parametrize(
    "text",
    [
        "",
        ECB_HEADER + "\n",
        ecb_csv(value="abc"),
        ecb_csv(value="0"),
        ecb_csv(value="-1.1"),
        ecb_csv(value="110.52"),  # a misplaced decimal point must not become the rate
        ecb_csv(value="NaN"),
        ecb_csv(day="13/11/2026"),
        ecb_csv(currency="GBP"),
        "<html>Service unavailable</html>",
    ],
)
def test_an_unreadable_or_implausible_answer_is_refused(text: str) -> None:
    with pytest.raises(ReferenceRateUnavailable):
        parse_ecb_csv(text)


async def test_the_fetcher_asks_the_ecb_for_the_latest_usd_observation() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text=ecb_csv(), headers={"content-type": "text/csv"})

    fetcher = EcbRateFetcher(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    rate = await fetcher()
    await fetcher.aclose()

    assert rate.usd_per_eur == Decimal("1.1052")
    [request] = seen
    assert request.url.host == "data-api.ecb.europa.eu"
    assert request.url.path == "/service/data/EXR/D.USD.EUR.SP00.A"
    assert dict(request.url.params) == {"lastNObservations": "1", "format": "csvdata"}


@pytest.mark.parametrize("failure", ["status", "network"])
async def test_a_failing_ecb_is_reported_not_raised_as_a_crash(failure: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if failure == "network":
            raise httpx2.ConnectError("no route to host")
        return httpx2.Response(503, text="maintenance")

    fetcher = EcbRateFetcher(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    with pytest.raises(ReferenceRateUnavailable):
        await fetcher()
    await fetcher.aclose()


# ------------------------------------------------------------------ applying


async def test_the_first_refresh_creates_the_rate_and_later_ones_version_it(
    session_factory: Sessions, clock: MutableClock
) -> None:
    state = RefreshState()
    friday = source_of("2026-11-13", "1.1052")

    first = await refresh_usd_eur_rate(session_factory, clock, friday, state=state)
    again = await refresh_usd_eur_rate(session_factory, clock, friday, state=state)
    clock.advance(days=3)
    monday = await refresh_usd_eur_rate(
        session_factory, clock, source_of("2026-11-16", "1.1100"), state=state
    )

    assert (first.outcome, again.outcome, monday.outcome) == (
        Outcome.UPDATED,
        Outcome.UNCHANGED,
        Outcome.UPDATED,
    )
    assert state.last is monday
    old, new = await usd_rates(session_factory)
    assert (old.rate, new.rate) == (Decimal("0.90481361"), Decimal("0.90090091"))
    assert old.effective_until == new.effective_from == clock.now()
    assert new.effective_until is None
    assert new.source == "ECB reference rate 2026-11-16: 1 EUR = 1.1100 USD"
    async with session_factory() as session:
        audit = (
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "exchange_rate.auto_update")
            )
        ).all()
    assert [(a.actor_type, a.actor_user_id) for a in audit] == [("system", None)] * 2
    assert audit[1].details["previous_rate"] == "0.90481361"


async def test_when_the_ecb_is_unreachable_the_rate_in_force_stays(
    session_factory: Sessions, clock: MutableClock
) -> None:
    await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-13", "1.1052"))
    clock.advance(days=1)

    result = await refresh_usd_eur_rate(session_factory, clock, RateSource(None))

    assert result.outcome is Outcome.UNAVAILABLE and not result.ok
    assert "unchanged" in result.detail
    [rate] = await usd_rates(session_factory)
    assert (rate.rate, rate.effective_until) == (Decimal("0.90481361"), None)


async def test_a_jump_of_more_than_ten_percent_is_not_applied_unattended(
    session_factory: Sessions, clock: MutableClock
) -> None:
    await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-13", "1.1052"))
    clock.advance(days=1)

    result = await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-16", "1.3000"))
    within = await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-16", "1.2000"))

    assert result.outcome is Outcome.REJECTED
    assert "by hand" in result.detail
    assert within.outcome is Outcome.UPDATED  # 8.6%: applied
    assert [r.rate for r in await usd_rates(session_factory)] == [
        Decimal("0.90481361"),
        Decimal("0.83333334"),
    ]


async def test_a_rate_set_by_hand_holds_until_the_ecb_publishes_for_that_day(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    """Sunday 15 November: the administrator sets a rate. The ECB's latest is Friday's,
    so the manual rate stays; Monday's publication replaces it — even though it differs
    by more than 10% from the manual figure (only ECB-to-ECB jumps are doubted)."""
    await register_and_login(client)
    clock.set(datetime(2026, 11, 15, 10, 0, tzinfo=UTC))
    await client.post(f"{API}/auth/refresh")
    manual = await client.post(
        f"{API}/admin/exchange-rates",
        json={"base_currency": "USD", "quote_currency": "EUR", "rate": "0.75", "source": "me"},
    )
    assert manual.status_code == 201, manual.text

    weekend = await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-13", "1.1052"))
    clock.set(datetime(2026, 11, 16, 15, 30, tzinfo=UTC))
    monday = await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-16", "1.1100"))

    assert weekend.outcome is Outcome.KEPT_MANUAL
    assert monday.outcome is Outcome.UPDATED
    assert [r.rate for r in await usd_rates(session_factory)] == [
        Decimal("0.75"),
        Decimal("0.90090091"),
    ]


async def test_a_rate_scheduled_by_hand_for_later_is_not_overridden(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    await register_and_login(client)
    scheduled = await client.post(
        f"{API}/admin/exchange-rates",
        json={
            "base_currency": "USD",
            "quote_currency": "EUR",
            "rate": "0.91",
            "effective_from": "2026-11-20T00:00:00Z",
        },
    )
    assert scheduled.status_code == 201, scheduled.text

    result = await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-13", "1.1052"))

    assert result.outcome is Outcome.KEPT_MANUAL
    assert len(await usd_rates(session_factory)) == 1


async def test_a_request_already_charged_keeps_the_rate_it_was_charged_at(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
) -> None:
    ids = await prepare_catalogue(session_factory, usd_to_eur=None)
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 1_000)
    await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-13", "1.1052"))
    fake_providers.anthropic.script(*reply("Antes.", usage=USAGE))
    fake_providers.anthropic.script(*reply("Depois.", usage=USAGE))

    await chat_events(await send_chat(client, ids[HAIKU]))
    clock.advance(minutes=5)
    await refresh_usd_eur_rate(session_factory, clock, source_of("2026-11-16", "1.0500"))
    await chat_events(await send_chat(client, ids[HAIKU]))

    before, after = await usage_rows(session_factory)
    assert (before.exchange_rate, after.exchange_rate) == (
        Decimal("0.90481361"),
        Decimal("0.95238096"),
    )
    # USD 0.00025 at each rate, rounded up to 8 places.
    assert (before.cost_eur, after.cost_eur) == (Decimal("0.00022621"), Decimal("0.00023810"))


# ------------------------------------------------------- start-up and the API


async def test_the_application_refreshes_at_start_up_when_the_source_is_the_ecb(
    make_app: AppFactory, session_factory: Sessions
) -> None:
    source = source_of("2026-11-13", "1.1052")
    async with make_app(exchange_rate_source="ecb", rate_fetcher=source) as app:
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(5):
                while app.state.rate_refresh.last is None:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        assert app.state.rate_refresh.last.outcome is Outcome.UPDATED

    assert source.calls == 1  # then it waits for the next interval
    [rate] = await usd_rates(session_factory)
    assert rate.source.startswith("ECB reference rate 2026-11-13")


async def test_in_manual_mode_nothing_is_fetched_at_start_up(
    make_app: AppFactory, session_factory: Sessions
) -> None:
    source = source_of("2026-11-13", "1.1052")
    # The test default is manual mode.
    async with make_app(rate_fetcher=source) as app, app.router.lifespan_context(app):
        await asyncio.sleep(0.05)

    assert source.calls == 0
    assert await usd_rates(session_factory) == []


async def test_the_administrator_can_see_the_rate_status_and_refresh_now(
    fresh_app: FastAPI,
    client: httpx.AsyncClient,
    make_client: ClientFactory,
    session_factory: Sessions,
    clock: MutableClock,
) -> None:
    app = fresh_app
    original = app.state.rate_fetcher
    app.state.rate_refresh.last = None
    await register_and_login(client)
    try:
        empty = (await client.get(f"{API}/admin/exchange-rates/status")).json()
        unreachable = await client.post(f"{API}/admin/exchange-rates/refresh")

        app.state.rate_fetcher = source_of("2026-11-13", "1.1052")
        refreshed = await client.post(f"{API}/admin/exchange-rates/refresh")
        clock.advance(hours=5)
        await client.post(f"{API}/auth/refresh")
        status = (await client.get(f"{API}/admin/exchange-rates/status")).json()

        async with make_client() as other:
            await second_user(app, other)
            forbidden = [
                (await other.get(f"{API}/admin/exchange-rates/status")).status_code,
                (await other.post(f"{API}/admin/exchange-rates/refresh")).status_code,
            ]
    finally:
        app.state.rate_fetcher = original
        app.state.rate_refresh.last = None

    assert empty == {
        "mode": "manual",
        "refresh_minutes": 180,
        "usd_eur": None,
        "last_refresh": None,
    }
    assert unreachable.status_code == 200
    assert unreachable.json()["outcome"] == "unavailable"
    assert refreshed.json()["outcome"] == "updated"
    assert status["usd_eur"]["rate"] == "0.90481361"
    assert status["usd_eur"]["source"] == "ECB reference rate 2026-11-13: 1 EUR = 1.1052 USD"
    assert status["usd_eur"]["age_hours"] == 5
    assert status["last_refresh"]["outcome"] == "updated"
    assert forbidden == [403, 403]
    async with session_factory() as session:
        entry = await session.scalar(
            select(AuditLog).where(AuditLog.action == "exchange_rate.auto_update")
        )
    assert entry is not None and (entry.actor_type, entry.actor_user_id is not None) == (
        "admin",
        True,
    )
