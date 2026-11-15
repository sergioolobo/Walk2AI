"""The AI gateway end to end: reserve -> call -> settle, through the HTTP API.

Providers are scripted fakes (see ``tests/ai_support.py``); no test reaches a real
provider or costs anything. Every test that moves credits ends by reconciling the
whole ledger.
"""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ai import AIModel, AIUsage, ChatMessage, ChatSession, ModelPricing
from app.models.enums import TransactionType
from app.models.wallet import WalletReservation, WalletTransaction
from app.services.ai.pricing import TokenUsage
from app.services.ai.providers import FailureCode, TextDelta, TurnRole
from app.services.wallet import WalletService
from tests.ai_support import (
    HAIKU,
    SONNET,
    Fail,
    FakeProviders,
    Gate,
    chat_events,
    expected_charge,
    expected_hold,
    prepare_catalogue,
    price_sheet,
    reply,
    send_chat,
    until,
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
DEFAULT_MAX_OUTPUT = 2048  # settings.ai_default_max_output_tokens


async def fund(session_factory: Sessions, clock: MutableClock, user_id: uuid.UUID, wc: int) -> None:
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).adjust(
            user_id, wc, kind=TransactionType.ADMIN_ADJUSTMENT, reason="test", actor_id=None
        )


async def usage_rows(session_factory: Sessions) -> list[AIUsage]:
    async with session_factory() as session:
        return list((await session.scalars(select(AIUsage).order_by(AIUsage.created_at))).all())


async def messages(session_factory: Sessions, chat_id: str) -> list[tuple[str, str, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(ChatMessage.role, ChatMessage.content, ChatMessage.status)
            .where(ChatMessage.session_id == uuid.UUID(chat_id))
            .order_by(ChatMessage.seq)
        )
        return [tuple(row) for row in rows.all()]  # type: ignore[misc]


@pytest.fixture
async def models(session_factory: Sessions) -> dict[str, uuid.UUID]:
    return await prepare_catalogue(session_factory)


@pytest.fixture
async def user(
    client: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> uuid.UUID:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 1_000)
    return account.id


# ----------------------------------------------------------------- the happy path


async def test_a_streamed_answer_is_charged_exactly_what_it_cost(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    sheet = await price_sheet(session_factory, models[HAIKU])
    fake_providers.anthropic.script(*reply("Olá! ", "Como posso ajudar?", usage=USAGE))
    request_id = uuid.uuid4()

    events = await chat_events(await send_chat(client, models[HAIKU], "Olá", request_id=request_id))

    hold = expected_hold(sheet, 1_000, DEFAULT_MAX_OUTPUT)
    charge = expected_charge(sheet, USAGE)
    # 1,000 in / 300 out on Haiku 5.5 at 0.92 EUR/USD: USD 0.00025 -> EUR 0.00023 -> 3 WC
    # (2.3 rounded up). The hold was the worst case: 1,000 prompt tokens at the dearest
    # input price plus 2,048 output tokens -> 12 WC.
    assert (charge, hold) == (3, 12)
    assert events["names"] == ["start", "delta", "delta", "usage", "done"]
    assert events["text"] == "Olá! Como posso ajudar?"
    start, usage = events["start"], events["usage"]
    assert start["request_id"] == str(request_id)
    assert start["estimate"]["worst_case_wc"] == hold
    assert start["estimate"]["input_tokens_basis"] == "provider_count"
    assert usage["charged_wc"] == charge
    assert usage["released_wc"] == hold - charge
    assert usage["tokens"] == {
        "input": 1_000,
        "cached_input": 0,
        "cache_write": 0,
        "output": 300,
        "reasoning": 0,
    }
    assert (usage["provider_cost"], usage["currency"], usage["cost_eur"]) == (
        "0.00025000",
        "USD",
        "0.00023000",
    )
    assert usage["wallet"] == {"available_wc": 1_000 - charge, "reserved_wc": 0}
    assert usage["cost_basis"] == "provider_usage"

    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1_000 - charge, 0)
    assert wallet.lifetime_spent == charge

    [row] = await usage_rows(session_factory)
    assert (row.status, row.credits_reserved, row.credits_charged) == ("succeeded", hold, charge)
    assert (row.input_tokens, row.output_tokens, row.stop_reason) == (1_000, 300, "end_turn")
    assert row.provider_request_id == "req_fake"
    assert (row.exchange_rate, row.wc_per_eur) == (Decimal("0.92"), Decimal(10_000))

    async with session_factory() as session:
        ledger = (
            await session.scalars(
                select(WalletTransaction).where(
                    WalletTransaction.transaction_type == TransactionType.AI_USAGE.value
                )
            )
        ).all()
        reservation = await session.scalar(
            select(WalletReservation).where(WalletReservation.request_id == request_id)
        )
    assert [tx.reference_id for tx in ledger] == [row.id]
    assert reservation is not None and reservation.status == "settled"
    assert reservation.settled_amount == charge

    assert await messages(session_factory, start["session_id"]) == [
        ("user", "Olá", "complete"),
        ("assistant", "Olá! Como posso ajudar?", "complete"),
    ]
    # The provider received the message — and nothing that could identify the user.
    [sent] = fake_providers.anthropic.requests
    assert [(t.role, t.content) for t in sent.turns] == [(TurnRole.USER, "Olá")]
    assert sent.model == HAIKU and sent.max_output_tokens == DEFAULT_MAX_OUTPUT
    await assert_ledger_consistent(session_factory, clock)


async def test_a_non_streamed_answer_carries_the_same_information(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.script(*reply("Sim.", usage=USAGE))

    response = await send_chat(client, models[HAIKU], "Pergunta", stream=False)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["text"], body["outcome"]) == ("Sim.", "usage")
    assert body["result"]["charged_wc"] == 3
    assert body["start"]["model"] == HAIKU
    await assert_ledger_consistent(session_factory, clock)


async def test_a_conversation_replays_its_complete_history(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    anthropic = fake_providers.anthropic
    anthropic.script(*reply("Lisboa.", usage=USAGE))
    anthropic.script(Fail(FailureCode.UNAVAILABLE))
    anthropic.script(*reply("Cerca de 545 mil.", usage=USAGE))

    first = await chat_events(await send_chat(client, models[HAIKU], "Capital de Portugal?"))
    chat_id = first["start"]["session_id"]
    failed = await chat_events(
        await send_chat(client, models[HAIKU], "Pergunta que falha", session_id=chat_id)
    )
    third = await chat_events(
        await send_chat(client, models[HAIKU], "Quantos habitantes?", session_id=chat_id)
    )

    assert failed["error"]["code"] == FailureCode.UNAVAILABLE
    assert third["start"]["session_id"] == chat_id
    # The failed question is kept (and shown) but not replayed to the model.
    assert [(t.role.value, t.content) for t in anthropic.requests[2].turns] == [
        ("user", "Capital de Portugal?"),
        ("assistant", "Lisboa."),
        ("user", "Quantos habitantes?"),
    ]
    assert await messages(session_factory, chat_id) == [
        ("user", "Capital de Portugal?", "complete"),
        ("assistant", "Lisboa.", "complete"),
        ("user", "Pergunta que falha", "error"),
        ("user", "Quantos habitantes?", "complete"),
        ("assistant", "Cerca de 545 mil.", "complete"),
    ]
    await assert_ledger_consistent(session_factory, clock)


# ------------------------------------------------------------ refused up front


async def test_insufficient_credits_are_refused_before_anything_is_sent(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
) -> None:
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 11)  # the worst case needs 12

    response = await send_chat(client, models[HAIKU])

    assert response.status_code == 402
    assert response.json()["code"] == "insufficient_credits"
    assert fake_providers.anthropic.requests == []
    assert await usage_rows(session_factory) == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatSession)) == 0
    wallet = await wallet_state(session_factory, account.id)
    assert (wallet.available_balance, wallet.reserved_balance) == (11, 0)


async def test_spending_limits_are_enforced_on_the_worst_case(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    await client.put(f"{API}/wallet/limits", json={"max_request_wc": 10})
    fake_providers.anthropic.script(*reply("Curto.", usage=USAGE))

    estimate = await client.post(
        f"{API}/ai/estimate", json={"model_id": str(models[HAIKU]), "message": "Olá"}
    )
    refused = await send_chat(client, models[HAIKU])
    smaller = await send_chat(client, models[HAIKU], max_output_tokens=1_000)

    assert estimate.status_code == 200
    assert estimate.json()["limit_refusal"] == "per_request"
    assert refused.status_code == 402
    assert refused.json()["code"] == "spending_limit_exceeded"
    # A smaller answer allowance makes the worst case (7 WC) fit under the limit.
    assert smaller.status_code == 200
    assert (await chat_events(smaller))["start"]["estimate"]["worst_case_wc"] == 7
    assert len(fake_providers.anthropic.requests) == 1


async def test_a_request_id_can_only_be_used_once(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.script(*reply("Uma vez.", usage=USAGE))
    request_id = uuid.uuid4()

    first = await send_chat(client, models[HAIKU], request_id=request_id)
    again = await send_chat(client, models[HAIKU], request_id=request_id)

    assert first.status_code == 200
    assert again.status_code == 409
    assert again.json()["code"] == "duplicate_request"
    assert len(fake_providers.anthropic.requests) == 1
    assert len(await usage_rows(session_factory)) == 1
    await assert_ledger_consistent(session_factory, clock)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("disabled", "disabled"),
        ("unchecked", "availability_not_checked"),
        ("no_key", "provider_not_configured"),
        ("unverified", "price_unverified"),
        ("no_fx", "no_exchange_rate"),
    ],
)
async def test_a_model_that_cannot_be_used_is_refused_with_the_reason(
    fresh_app: Any,
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    user: uuid.UUID,
    setup: str,
    reason: str,
) -> None:
    model = HAIKU
    ids = await prepare_catalogue(
        session_factory,
        enable=() if setup == "disabled" else (model,),
        usd_to_eur=None if setup == "no_fx" else Decimal("0.92"),
    )
    if setup == "unchecked":
        async with session_factory() as session, session.begin():
            await session.execute(
                update(AIModel).where(AIModel.id == ids[model]).values(availability_checked_at=None)
            )
    if setup == "no_key":
        fresh_app.state.ai_providers.pop("anthropic")
    if setup == "unverified":
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ModelPricing)
                .where(ModelPricing.model_id == ids[model])
                .values(verified=False, verified_at=None)
            )

    response = await send_chat(client, ids[model])
    listed = (await client.get(f"{API}/ai/models")).json()

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "model_unavailable"
    assert reason in response.json()["reasons"]
    assert str(ids[model]) not in {m["id"] for m in listed}
    assert await usage_rows(session_factory) == []


async def test_options_the_model_does_not_support_are_refused(
    client: httpx.AsyncClient,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    temperature = await send_chat(client, models[HAIKU], temperature="0.5")
    too_long = await send_chat(client, models[HAIKU], max_output_tokens=100_000)

    assert temperature.status_code == 422
    assert "temperature" in temperature.json()["detail"]
    assert too_long.status_code == 422
    assert "max_output_tokens" in too_long.json()["detail"]
    assert fake_providers.anthropic.requests == []


async def test_a_conversation_longer_than_the_context_window_is_refused(
    client: httpx.AsyncClient,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.prompt_tokens = 1_000_000  # the context limit (1M) minus nothing

    response = await send_chat(client, models[HAIKU])

    assert response.status_code == 422
    assert response.json()["code"] == "conversation_too_long"


async def test_token_counting_falls_back_to_a_safe_upper_bound(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.count_failure = FailureCode.RATE_LIMITED
    fake_providers.anthropic.script(*reply("Ok.", usage=USAGE))
    message = "Olá — com acentos: ç, ã, é."  # multi-byte characters count by bytes

    events = await chat_events(await send_chat(client, models[HAIKU], message))

    estimate = events["start"]["estimate"]
    assert estimate["input_tokens_basis"] == "local_upper_bound"
    assert estimate["input_tokens"] == len(message.encode()) + 8 + 16
    assert events["usage"]["charged_wc"] == 3  # still charged on reported usage
    await assert_ledger_consistent(session_factory, clock)


async def test_a_rejected_api_key_fails_before_any_credits_are_held(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.count_failure = FailureCode.AUTH

    response = await send_chat(client, models[HAIKU])

    assert response.status_code == 502
    assert response.json()["code"] == "provider_error"
    assert response.json()["provider_code"] == FailureCode.AUTH
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1_000, 0)


# ---------------------------------------------------------- failures and outcomes


async def test_a_provider_error_before_any_text_charges_nothing(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.script(Fail(FailureCode.RATE_LIMITED))

    events = await chat_events(await send_chat(client, models[HAIKU], "Olá"))

    error = events["error"]
    assert (error["code"], error["status"]) == (FailureCode.RATE_LIMITED, "failed")
    assert error["charged_wc"] == 0
    assert error["released_wc"] == 12
    assert error["wallet"] == {"available_wc": 1_000, "reserved_wc": 0}
    [row] = await usage_rows(session_factory)
    assert (row.status, row.credits_charged, row.cost_basis) == ("failed", 0, "none")
    assert await messages(session_factory, events["start"]["session_id"]) == [
        ("user", "Olá", "error")
    ]
    await assert_ledger_consistent(session_factory, clock)


async def test_a_failure_with_reported_usage_charges_that_usage(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    billed = TokenUsage(input_tokens=1_000, output_tokens=50)
    fake_providers.anthropic.script(
        TextDelta("Parcial"), Fail(FailureCode.FAILED, billed_usage=billed)
    )

    events = await chat_events(await send_chat(client, models[HAIKU]))

    sheet = await price_sheet(session_factory, models[HAIKU])
    assert events["error"]["charged_wc"] == expected_charge(sheet, billed)
    assert events["error"]["cost_basis"] == "provider_usage"
    [row] = await usage_rows(session_factory)
    assert (row.status, row.output_tokens) == ("failed", 50)
    chat_id = events["start"]["session_id"]
    assert (await messages(session_factory, chat_id))[-1] == ("assistant", "Parcial", "partial")
    await assert_ledger_consistent(session_factory, clock)


async def test_a_stream_cut_off_without_usage_charges_the_reserved_worst_case(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """Generation began and the provider never said what it used. The provider will
    still bill it, so the worst case is charged: real spend can never exceed credits."""
    fake_providers.anthropic.script(TextDelta("Começo"), Fail(FailureCode.INCOMPLETE))

    events = await chat_events(await send_chat(client, models[HAIKU]))

    error = events["error"]
    assert (error["charged_wc"], error["released_wc"], error["cost_basis"]) == (12, 0, "estimated")
    [row] = await usage_rows(session_factory)
    assert (row.status, row.credits_charged, row.cost_basis) == ("failed", 12, "estimated")
    assert row.provider_cost is not None and row.provider_cost == row.estimated_cost
    await assert_ledger_consistent(session_factory, clock)


async def test_a_completed_stream_without_usage_is_treated_as_cut_off(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    fake_providers.anthropic.script(TextDelta("Sem fim"))  # the stream just ends

    events = await chat_events(await send_chat(client, models[HAIKU]))

    assert events["error"]["code"] == FailureCode.INCOMPLETE
    assert events["error"]["charged_wc"] == 12
    await assert_ledger_consistent(session_factory, clock)


async def test_a_refusal_with_usage_is_an_answer_and_is_charged(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    usage = TokenUsage(input_tokens=1_000, output_tokens=12)
    fake_providers.anthropic.script(*reply("Não posso ajudar.", usage=usage, stop_reason="refusal"))

    events = await chat_events(await send_chat(client, models[HAIKU]))

    assert events["usage"]["stop_reason"] == "refusal"
    assert events["usage"]["charged_wc"] == 1  # tiny, but never free: at least 1 WC
    await assert_ledger_consistent(session_factory, clock)


# --------------------------------------------------- disconnects and concurrency


async def test_a_disconnected_client_does_not_stop_or_escape_the_charge(
    fresh_app: Any,
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """The browser goes away after the first words. The request still finishes on the
    server, is charged exactly, and the full answer is in the conversation."""
    gate = Gate()
    fake_providers.anthropic.script(*reply("Primeira ", "parte e resto.", usage=USAGE, gate=gate))
    request_id = uuid.uuid4()
    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    body = (
        f'{{"request_id":"{request_id}","model_id":"{models[HAIKU]}","message":"Olá"}}'
    ).encode()

    sent: list[dict[str, Any]] = []
    first_text = asyncio.Event()
    body_read = False

    async def receive() -> dict[str, Any]:
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_text.wait()  # the browser goes away once the first words arrive
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and b"event: delta" in message.get("body", b""):
            first_text.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"{API}/ai/chat",
        "raw_path": f"{API}/ai/chat".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"cookie", cookies.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    await fresh_app(scope, receive, send)

    # The HTTP exchange is over; the request is still running on the server.
    [task] = list(fresh_app.state.ai_inflight)
    assert not task.done()
    gate.open()
    await task

    [row] = await usage_rows(session_factory)
    assert (row.status, row.credits_charged) == ("succeeded", 3)
    chat_id = str(row.session_id)
    assert await messages(session_factory, chat_id) == [
        ("user", "Olá", "complete"),
        ("assistant", "Primeira parte e resto.", "complete"),
    ]
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (997, 0)
    await assert_ledger_consistent(session_factory, clock)


async def test_concurrent_requests_can_never_hold_more_than_the_wallet(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
) -> None:
    """Ten requests at once against a wallet that covers three worst cases: exactly
    three are let through, and the rest are refused before reaching the provider."""
    account = await register_and_login(client)
    await fund(session_factory, clock, account.id, 3 * 12 + 5)
    gate = Gate()
    for _ in range(10):
        fake_providers.anthropic.script(*reply("Sim", " — feito.", usage=USAGE, gate=gate))

    async def attempt() -> int:
        return (await send_chat(client, models[HAIKU])).status_code

    racing = [asyncio.create_task(attempt()) for _ in range(10)]
    # Let every request reserve (or be refused), then let the answers finish.
    await until(lambda: sum(t.done() for t in racing) == 7)
    held = await wallet_state(session_factory, account.id)
    assert (held.available_balance, held.reserved_balance) == (5, 36)
    gate.open()
    statuses = await asyncio.gather(*racing)

    assert sorted(statuses) == [200] * 3 + [402] * 7
    assert len(fake_providers.anthropic.requests) == 3
    wallet = await wallet_state(session_factory, account.id)
    assert (wallet.available_balance, wallet.reserved_balance) == (41 - 3 * 3, 0)
    await assert_ledger_consistent(session_factory, clock)


async def test_deleting_a_conversation_mid_answer_still_settles_the_charge(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fresh_app: Any,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    gate = Gate()
    fake_providers.anthropic.script(*reply("A meio", " e fim.", usage=USAGE, gate=gate))

    pending = asyncio.create_task(send_chat(client, models[HAIKU]))
    await gate.reached.wait()
    [row] = await usage_rows(session_factory)
    deleted = await client.delete(f"{API}/ai/sessions/{row.session_id}")
    gate.open()
    await pending

    assert deleted.status_code == 204
    [row] = await usage_rows(session_factory)
    assert (row.status, row.credits_charged, row.session_id) == ("succeeded", 3, None)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0
    await assert_ledger_consistent(session_factory, clock)


async def test_shutdown_settles_requests_in_progress(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fresh_app: Any,
    fake_providers: FakeProviders,
    models: dict[str, uuid.UUID],
    user: uuid.UUID,
) -> None:
    """A request that will not finish in the grace period is cancelled and settled
    conservatively: it had started generating, so the worst case is charged."""
    from app.services.ai.gateway import wait_for_inflight

    stuck = Gate()  # never opened: the answer stalls after its first words
    fake_providers.anthropic.script(TextDelta("Interrompido"), stuck)
    pending = asyncio.create_task(send_chat(client, models[HAIKU]))
    await stuck.reached.wait()  # the first words have been received

    await wait_for_inflight(fresh_app.state.ai_inflight, 0)
    events = await chat_events(await pending)

    assert events["error"]["status"] == "cancelled"
    assert events["error"]["code"] == "server_shutdown"
    assert events["error"]["charged_wc"] == 12
    await assert_ledger_consistent(session_factory, clock)


# ------------------------------------------------------------ other providers


async def test_each_provider_is_called_with_its_own_model(
    client: httpx.AsyncClient,
    session_factory: Sessions,
    clock: MutableClock,
    fake_providers: FakeProviders,
    user: uuid.UUID,
) -> None:
    ids = await prepare_catalogue(session_factory, enable=(HAIKU, SONNET))
    fake_providers.anthropic.script(*reply("Haiku.", usage=USAGE))
    fake_providers.anthropic.script(*reply("Sonnet.", usage=USAGE))

    haiku = await chat_events(await send_chat(client, ids[HAIKU]))
    sonnet = await chat_events(await send_chat(client, ids[SONNET]))

    assert [r.model for r in fake_providers.anthropic.requests] == [HAIKU, SONNET]
    # Same tokens, twenty times the price: USD 0.005 -> EUR 0.0046 -> 46 WC (Haiku: 2.3 -> 3).
    sheet = await price_sheet(session_factory, ids[SONNET])
    assert sonnet["usage"]["charged_wc"] == expected_charge(sheet, USAGE) == 46
    assert haiku["usage"]["charged_wc"] == 3
    await assert_ledger_consistent(session_factory, clock)
