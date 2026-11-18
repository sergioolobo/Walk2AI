"""AI test support: a scripted provider and a ready-to-use catalogue.

``FakeProvider`` stands in for a provider adapter: it yields exactly the events a real
adapter produces (``Started``, ``TextDelta``, ``Completed``) or raises the same
``ProviderFailure`` a real one raises, at the moment the script says. The adapters'
own translation of real SDK traffic is tested separately (``test_ai_providers``), and
one end-to-end test runs the real Anthropic adapter behind the gateway.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ai import AIModel, ExchangeRate
from app.seeds import seed_initial_configuration
from app.services.ai.pricing import (
    Conversion,
    PriceSheet,
    TokenUsage,
    charge_for,
    credits_for,
    worst_case_cost,
)
from app.services.ai.providers import (
    Completed,
    FailureCode,
    ModelCheck,
    ProviderEvent,
    ProviderFailure,
    ProviderRequest,
    Started,
    TextDelta,
)
from tests.support import API

Sessions = async_sessionmaker[AsyncSession]

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
LUNA = "gpt-6-luna"
USD_TO_EUR = Decimal("0.92")
WC_PER_EUR = Decimal("10000")
CONVERSION = Conversion(to_eur=USD_TO_EUR, wc_per_eur=WC_PER_EUR)
RATES_FROM = datetime(2026, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------ the script


@dataclass
class Gate:
    """Pauses a stream at a point until the test opens it."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    opened: asyncio.Event = field(default_factory=asyncio.Event)

    def open(self) -> None:
        self.opened.set()


@dataclass
class Fail:
    """Raise this failure at this point in the stream."""

    code: FailureCode
    billed_usage: TokenUsage | None = None


@dataclass
class Hang:
    """Never finish (until cancelled or timed out)."""


Step = ProviderEvent | Gate | Fail | Hang


def reply(
    *parts: str,
    usage: TokenUsage,
    stop_reason: str = "end_turn",
    request_id: str = "req_fake",
    gate: Gate | None = None,
) -> list[Step]:
    """A normal answer. With ``gate``, the stream pauses after the first piece of text."""
    steps: list[Step] = [Started(request_id)]
    for index, part in enumerate(parts):
        steps.append(TextDelta(part))
        if index == 0 and gate is not None:
            steps.append(gate)
    steps.append(Completed(usage, stop_reason, request_id, None))
    return steps


class FakeProvider:
    """A provider adapter driven by scripts, one per request, in order."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.scripts: list[list[Step]] = []
        self.requests: list[ProviderRequest] = []
        self.counted: list[ProviderRequest] = []
        self.prompt_tokens: int | None = 1_000  # what count_input_tokens answers
        self.count_failure: FailureCode | None = None
        self.check: ModelCheck = ModelCheck(available=True)
        self.check_failure: FailureCode | None = None
        self.checked: list[str] = []
        self.closed = False

    def script(self, *steps: Step) -> None:
        self.scripts.append(list(steps))

    async def count_input_tokens(self, request: ProviderRequest) -> int:
        self.counted.append(request)
        if self.count_failure is not None:
            raise ProviderFailure(self.count_failure)
        assert self.prompt_tokens is not None
        return self.prompt_tokens

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        if not self.scripts:
            raise AssertionError(f"{self.provider}: no script for this request")
        for step in self.scripts.pop(0):
            if isinstance(step, Gate):
                step.reached.set()
                await step.opened.wait()
            elif isinstance(step, Fail):
                raise ProviderFailure(step.code, billed_usage=step.billed_usage)
            elif isinstance(step, Hang):
                await asyncio.Event().wait()
            else:
                yield step

    async def check_model(self, model: str) -> ModelCheck:
        self.checked.append(model)
        if self.check_failure is not None:
            raise ProviderFailure(self.check_failure)
        return self.check

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeProviders:
    anthropic: FakeProvider
    openai: FakeProvider


# ------------------------------------------------------------------ catalogue


async def prepare_catalogue(
    session_factory: Sessions,
    *,
    enable: Sequence[str] = (HAIKU,),
    usd_to_eur: Decimal | None = USD_TO_EUR,
) -> dict[str, uuid.UUID]:
    """Seed the catalogue, add a USD->EUR rate, and make ``enable`` usable. Idempotent.

    Rates start on 2026-01-01 so tests may move the clock anywhere in 2026. Returns
    model ids by identifier.
    """
    async with session_factory() as session, session.begin():
        await seed_initial_configuration(session)
        has_rate = await session.scalar(
            select(ExchangeRate.id).where(
                ExchangeRate.base_currency == "USD", ExchangeRate.quote_currency == "EUR"
            )
        )
        if usd_to_eur is not None and has_rate is None:
            session.add(
                ExchangeRate(
                    base_currency="USD",
                    quote_currency="EUR",
                    rate=usd_to_eur,
                    effective_from=RATES_FROM,
                    source="test",
                )
            )
        if enable:
            await session.execute(
                update(AIModel)
                .where(AIModel.model_identifier.in_(enable))
                .values(enabled=True, availability_checked_at=RATES_FROM)
            )
        rows = (await session.execute(select(AIModel.model_identifier, AIModel.id))).all()
    return {identifier: model_id for identifier, model_id in rows}


async def price_sheet(session_factory: Sessions, model_id: uuid.UUID) -> PriceSheet:
    from app.services.ai.catalogue import price_in_force

    async with session_factory() as session:
        row = await price_in_force(session, model_id, datetime(2026, 6, 1, tzinfo=UTC))
        assert row is not None
        return PriceSheet.from_row(row)


def expected_charge(sheet: PriceSheet, usage: TokenUsage) -> int:
    return charge_for(usage, sheet, CONVERSION).credits


def expected_hold(sheet: PriceSheet, prompt_tokens: int, max_output: int) -> int:
    return max(1, credits_for(worst_case_cost(prompt_tokens, max_output, sheet), CONVERSION))


# ------------------------------------------------------------------ HTTP


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = "", "{}"
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((name, json.loads(data)))
    return events


async def send_chat(
    client: httpx.AsyncClient,
    model_id: uuid.UUID,
    message: str = "Hello",
    *,
    request_id: uuid.UUID | None = None,
    session_id: uuid.UUID | str | None = None,
    stream: bool = True,
    **extra: Any,
) -> httpx.Response:
    body: dict[str, Any] = {
        "request_id": str(request_id or uuid.uuid4()),
        "model_id": str(model_id),
        "message": message,
        "stream": stream,
        **extra,
    }
    if session_id is not None:
        body["session_id"] = str(session_id)
    return await client.post(f"{API}/ai/chat", json=body)


async def chat_events(response: httpx.Response) -> dict[str, Any]:
    """The events of a streamed answer, by name (deltas joined into ``text``)."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "done", names
    result: dict[str, Any] = {"names": names, "text": ""}
    for name, data in events:
        if name == "delta":
            result["text"] += data["text"]
        else:
            result[name] = data
    return result


async def until(condition: Callable[[], bool], *, seconds: float = 5) -> None:
    """Wait for something another task does (polling: there is no event to await)."""
    for _ in range(int(seconds * 100)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def second_user(
    app: Any, client: httpx.AsyncClient, email: str = "stranger@example.com"
) -> uuid.UUID:
    """Sign ``client`` in as another user (registration closes after the first one)."""
    from app.models.user import User
    from app.services.wallet import WalletService
    from tests.support import DEFAULT_PASSWORD, Account, login

    session_factory: Sessions = app.state.session_factory
    clock = app.state.clock
    async with session_factory() as session, session.begin():
        now = clock.now()
        user = User(
            email=email,
            password_hash=app.state.passwords.hash(DEFAULT_PASSWORD),
            timezone="Europe/Lisbon",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        await WalletService(session, clock).create_wallet(user.id)
        user_id = user.id
    await login(client, Account(id=user_id, email=email, password=DEFAULT_PASSWORD, role="user"))
    return user_id
