"""The gateway below the HTTP layer: time limits, abandoned requests, the real SDK
adapters behind it, estimates and conversation management."""

import uuid
from decimal import Decimal
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.ai import AIUsage
from app.models.enums import TransactionType
from app.models.user import User
from app.services.ai.gateway import AIGateway, ChatCommand
from app.services.ai.pricing import TokenUsage, stored, to_eur, worst_case_cost
from app.services.ai.providers import AnthropicAdapter, FailureCode, OpenAIAdapter, TextDelta
from app.services.maintenance import run_maintenance_pass
from app.services.wallet import WalletService
from tests.ai_support import (
    CONVERSION,
    HAIKU,
    LUNA,
    RATES_FROM,
    FakeProvider,
    FakeProviders,
    Hang,
    chat_events,
    expected_charge,
    expected_hold,
    prepare_catalogue,
    price_sheet,
    reply,
    second_user,
    send_chat,
)
from tests.conftest import ClientFactory
from tests.provider_fixtures import (
    RecordingProvider,
    anthropic_stream,
    openai_stream,
    openai_usage,
    stream_response,
)
from tests.support import (
    API,
    MutableClock,
    assert_ledger_consistent,
    register_and_login,
    wallet_state,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

USAGE = TokenUsage(input_tokens=1_000, output_tokens=300)


async def fund(session_factory: Sessions, clock: MutableClock, user_id: uuid.UUID, wc: int) -> None:
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).adjust(
            user_id, wc, kind=TransactionType.ADMIN_ADJUSTMENT, reason="test", actor_id=None
        )


async def load_user(session_factory: Sessions, user_id: uuid.UUID) -> User:
    async with session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


@pytest.fixture
async def models(session_factory: Sessions) -> dict[str, uuid.UUID]:
    return await prepare_catalogue(session_factory, enable=(HAIKU, LUNA))


@pytest.fixture
async def user(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> uuid.UUID:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 1_000)
    return account.id


def gateway_with(
    fresh_app: FastAPI, settings: Settings, provider: Any, *, timeout: float
) -> AIGateway:
    """A gateway with a short time limit (the setting's validated minimum is 5 s)."""
    return AIGateway(
        fresh_app.state.session_factory,
        settings=settings.model_copy(update={"ai_request_timeout_seconds": timeout}),
        clock=fresh_app.state.clock,
        providers={"anthropic": provider},
        inflight=set(),
    )


# --------------------------------------------------------------- time limits


@pytest.mark.parametrize(
    ("script", "status", "charged"),
    [
        ([Hang()], "unknown", 0),  # nothing arrived: released, flagged for reconciliation
        ([TextDelta("Começou"), Hang()], "timeout", 12),  # generating: worst case charged
    ],
)
async def test_a_request_that_runs_out_of_time(
    fresh_app: FastAPI,
    settings: Settings,
    session_factory: Sessions,
    clock: MutableClock,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
    script: list[Any],
    status: str,
    charged: int,
) -> None:
    provider = FakeProvider("anthropic")
    provider.script(*script)
    gateway = gateway_with(fresh_app, settings, provider, timeout=0.2)
    command = ChatCommand(
        request_id=uuid.uuid4(), session_id=None, model_id=models[HAIKU], message="Olá"
    )

    handle = await gateway.start(await load_user(session_factory, user), command, stream=True)
    events = [event async for event in handle.events()]
    await handle.task

    name, error = events[-1]
    assert name == "error"
    assert (error["status"], error["code"], error["charged_wc"]) == (
        status,
        FailureCode.TIMEOUT,
        charged,
    )
    async with session_factory() as session:
        row = await session.scalar(select(AIUsage))
    assert row is not None and (row.status, row.credits_charged) == (status, charged)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1_000 - charged, 0)
    await assert_ledger_consistent(session_factory, clock)


class CrashingGateway(AIGateway):
    """Launches requests that never run: as if the process died right after reserving."""

    async def _run(self, context: Any, queue: Any) -> None:
        return None


async def test_a_request_whose_process_died_is_released_and_flagged(
    fresh_app: FastAPI,
    settings: Settings,
    session_factory: Sessions,
    clock: MutableClock,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """The hold outlives the request's time limit plus a margin (60 s + 300 s here), then
    the maintenance pass releases it — nothing charged — and flags the record."""
    gateway = CrashingGateway(
        session_factory,
        settings=settings.model_copy(update={"ai_request_timeout_seconds": 60}),
        clock=clock,
        providers={"anthropic": FakeProvider("anthropic")},
        inflight=set(),
    )
    command = ChatCommand(
        request_id=uuid.uuid4(), session_id=None, model_id=models[HAIKU], message="Olá"
    )
    handle = await gateway.start(await load_user(session_factory, user), command, stream=True)
    await handle.task

    clock.advance(minutes=5)
    await run_maintenance_pass(session_factory, clock)
    async with session_factory() as session:
        assert await session.scalar(select(AIUsage.status)) == "pending"
    assert (await wallet_state(session_factory, user)).reserved_balance == 12

    clock.advance(minutes=2)
    await run_maintenance_pass(session_factory, clock)

    async with session_factory() as session:
        row = await session.scalar(select(AIUsage))
    assert row is not None
    assert (row.status, row.error_code, row.credits_charged) == ("unknown", "abandoned", 0)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1_000, 0)
    await assert_ledger_consistent(session_factory, clock)


# --------------------------------------------------- the real adapters behind it


async def test_end_to_end_through_the_real_anthropic_sdk(
    fresh_app: FastAPI,
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """The official SDK parses a recorded-shape stream; the gateway charges what it
    reports. Thinking tokens are part of the output and billed once."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 42})
        assert request.url.path == "/v1/messages"
        return stream_response(
            anthropic_stream(["Olá", ", mundo"], input_tokens=42, output_tokens=120)
        )

    recorder = RecordingProvider(handler)
    adapter = AnthropicAdapter(
        "sk-ant-test", timeout_seconds=30, http_client=recorder.client(), max_retries=0
    )
    providers = fresh_app.state.ai_providers
    providers["anthropic"] = adapter
    try:
        events = await chat_events(await send_chat(client, models[HAIKU], "Diz olá"))
    finally:
        providers.pop("anthropic")

    usage = TokenUsage(input_tokens=42, output_tokens=120, reasoning_tokens=80)
    sheet = await price_sheet(session_factory, models[HAIKU])
    assert events["text"] == "Olá, mundo"
    assert events["usage"]["tokens"]["reasoning"] == 80
    assert events["usage"]["charged_wc"] == expected_charge(sheet, usage) == 1
    sent = recorder.body()
    assert sent["model"] == HAIKU and sent["stream"] is True
    assert sent["messages"] == [{"role": "user", "content": "Diz olá"}]
    assert "temperature" not in sent
    # The API key goes to the provider and nowhere else.
    assert recorder.requests[-1].headers["authorization"] == "Bearer sk-ant-test"
    await assert_ledger_consistent(session_factory, clock)


async def test_end_to_end_through_the_real_openai_sdk(
    fresh_app: FastAPI,
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """OpenAI reports cached tokens inside input tokens; they are priced at the cached
    rate, not twice. (The model's price is unverified, so it is verified here.)"""
    from sqlalchemy import update

    from app.models.ai import ModelPricing

    async with session_factory() as session, session.begin():
        await session.execute(
            update(ModelPricing)
            .where(ModelPricing.model_id == models[LUNA])
            .values(
                verified=True, verified_at=RATES_FROM, source_url="https://example.com/test-only"
            )
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            return httpx2.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 90}
            )
        return stream_response(
            openai_stream(["Olá", "!"], usage=openai_usage(90, 40, cached=64, reasoning=10))
        )

    recorder = RecordingProvider(handler)
    adapter = OpenAIAdapter(
        "sk-test", timeout_seconds=30, http_client=recorder.client(), max_retries=0
    )
    providers = fresh_app.state.ai_providers
    providers["openai"] = adapter
    try:
        events = await chat_events(await send_chat(client, models[LUNA], "Olá"))
    finally:
        providers.pop("openai")

    assert events["text"] == "Olá!"
    assert events["usage"]["tokens"] == {
        "input": 26,
        "cached_input": 64,
        "cache_write": 0,
        "output": 40,
        "reasoning": 10,
    }
    sent = recorder.body()
    assert (sent["store"], sent["stream"]) == (False, True)
    await assert_ledger_consistent(session_factory, clock)


# ------------------------------------------------------------------ estimates


async def test_an_estimate_shows_the_worst_case_and_reserves_nothing(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.prompt_tokens = 5_000
    response = await client.post(
        f"{API}/ai/estimate",
        json={"model_id": str(models[HAIKU]), "message": "Olá", "max_output_tokens": 4_000},
    )

    sheet = await price_sheet(session_factory, models[HAIKU])
    worst = worst_case_cost(5_000, 4_000, sheet)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["input_tokens"] == 5_000 and body["max_output_tokens"] == 4_000
    # 5,000 prompt tokens at the dearest input price (1h cache write, USD 0.20/M) plus
    # 4,000 output tokens at USD 0.50/M = USD 0.003 -> EUR 0.00276 -> 27.6 -> 28 WC.
    assert body["worst_case"] == {
        "cost": format(stored(worst), "f"),
        "currency": "USD",
        "cost_eur": format(stored(to_eur(worst, CONVERSION)), "f"),
        "credits_wc": expected_hold(sheet, 5_000, 4_000),
    }
    assert body["worst_case"]["credits_wc"] == 28
    assert (body["available_wc"], body["can_afford"], body["limit_refusal"]) == (
        1_000,
        True,
        None,
    )
    wallet = await wallet_state(session_factory, user)
    assert wallet.reserved_balance == 0
    assert fake_providers.anthropic.requests == []


# -------------------------------------------------------------- conversations


async def test_conversations_can_be_listed_read_renamed_and_deleted(
    fresh_app: FastAPI,
    client: httpx.AsyncClient,
    make_client: ClientFactory,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.script(*reply("Resposta.", usage=USAGE))
    events = await chat_events(
        await send_chat(client, models[HAIKU], "Uma pergunta bastante longa " * 5)
    )
    chat_id = events["start"]["session_id"]
    clock.advance(minutes=1)
    empty = await client.post(f"{API}/ai/sessions", json={"model_id": str(models[HAIKU])})

    listed = (await client.get(f"{API}/ai/sessions")).json()
    detail = (await client.get(f"{API}/ai/sessions/{chat_id}")).json()
    renamed = await client.patch(f"{API}/ai/sessions/{chat_id}", json={"title": "Renomeada"})

    assert empty.status_code == 201
    assert [s["id"] for s in listed] == [empty.json()["id"], chat_id]
    first = next(s for s in listed if s["id"] == chat_id)
    assert first["title"].endswith("…") and len(first["title"]) == 60
    assert (first["messages"], first["credits_charged_wc"]) == (2, 3)
    assert [m["role"] for m in detail["items"]] == ["user", "assistant"]
    assert detail["items"][0]["usage"] is None
    assert detail["items"][1]["usage"]["credits_charged_wc"] == 3
    assert detail["items"][1]["usage"]["provider_cost"] == "0.00025000"
    assert renamed.json()["title"] == "Renomeada"

    async with make_client() as stranger:
        other = await second_user(fresh_app, stranger)
        await fund(session_factory, clock, other, 100)
        assert (await stranger.get(f"{API}/ai/sessions/{chat_id}")).status_code == 404
        assert (await stranger.delete(f"{API}/ai/sessions/{chat_id}")).status_code == 404
        assert (await stranger.get(f"{API}/ai/sessions")).json() == []
        # Nor can a message be added to someone else's conversation.
        borrowed = await send_chat(stranger, models[HAIKU], session_id=chat_id)
        assert borrowed.status_code == 404
        assert (await wallet_state(session_factory, other)).reserved_balance == 0

    assert (await client.delete(f"{API}/ai/sessions/{chat_id}")).status_code == 204
    assert (await client.get(f"{API}/ai/sessions/{chat_id}")).status_code == 404
    async with session_factory() as session:
        kept = await session.scalar(select(AIUsage))
    # The cost record stays (the ledger points at it), without the conversation.
    assert kept is not None and kept.session_id is None and kept.credits_charged == 3
    await assert_ledger_consistent(session_factory, clock)


async def test_ai_routes_require_a_signed_in_user(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("GET", "/ai/providers"),
        ("GET", "/ai/models"),
        ("POST", "/ai/estimate"),
        ("POST", "/ai/chat"),
        ("GET", "/ai/sessions"),
        ("GET", f"/ai/sessions/{uuid.uuid4()}"),
    ]:
        response = await client.request(method, f"{API}{path}", json={})
        assert response.status_code == 401, (method, path)


async def test_providers_report_only_whether_a_key_is_configured(
    client: httpx.AsyncClient, fake_providers: FakeProviders, user: uuid.UUID
) -> None:
    response = await client.get(f"{API}/ai/providers")

    assert response.json() == [
        {"provider": "anthropic", "configured": True},
        {"provider": "openai", "configured": True},
    ]


async def test_the_model_list_shows_usable_models_with_their_prices(
    client: httpx.AsyncClient,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    listed = (await client.get(f"{API}/ai/models")).json()

    # gpt-6-luna is enabled but its price is unverified: it is not offered.
    assert [m["model"] for m in listed] == [HAIKU]
    [haiku] = listed
    assert haiku["prices"]["input"] == "0.10000000"
    assert haiku["prices"]["output"] == "0.50000000"
    assert haiku["prices"]["long_context_threshold"] == 100_000
    assert haiku["prices"]["verified"] is True
    assert haiku["supports_temperature"] is False
    assert (haiku["max_output_tokens"], haiku["default_max_output_tokens"]) == (8192, 2048)
    assert Decimal(haiku["prices"]["output"]) > 0
