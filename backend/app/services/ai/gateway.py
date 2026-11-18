"""The AI gateway: one request, from estimate to settled charge.

    plan     count the prompt, find the model's verified price, compute the worst case
    reserve  hold that many credits (limits checked under the wallet lock) — COMMITTED
             before anything is sent, so a request can never outspend the wallet
    call     stream from the provider; no database transaction is open meanwhile
    settle   charge the exact cost from the provider's reported usage and release the
             rest of the hold — one transaction, with the usage record and the answer

The provider call runs in its own task, decoupled from the HTTP response. If the browser
disconnects, the request still finishes on the server: it is charged exactly and the
answer is saved to the conversation, instead of being cut off at an unknown cost.

Outcomes (see docs/ARCHITECTURE.md, "Outcome matrix"):

    provider reported usage (success, refusal, truncation, or failure with usage)
        → charged exactly what the usage costs
    provider refused the request before generating (HTTP error)
        → nothing charged, hold released
    timed out or cut off after generation began, usage unknown
        → charged the hold (the worst case): real spend can never exceed credits
    timed out before anything arrived
        → nothing charged, released, recorded as "unknown" for reconciliation

This module owns its transactions (``session.begin()`` blocks): it is a long-running
orchestrator, not a step inside someone else's unit of work.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import AppError, ValidationFailed
from app.core.timeutil import day_bounds_utc, local_date, month_bounds_utc
from app.models.ai import AIModel, AIUsage, ChatMessage, ChatSession
from app.models.user import User
from app.models.wallet import SpendingLimits, WalletAccount
from app.services.ai import catalogue, chat
from app.services.ai.pricing import (
    TokenUsage,
    charge_for,
    credits_for,
    stored,
    to_eur,
    worst_case_cost,
)
from app.services.ai.providers import (
    Completed,
    FailureCode,
    ProviderAdapter,
    ProviderFailure,
    ProviderRequest,
    Started,
    TextDelta,
    Turn,
    TurnRole,
)
from app.services.wallet import (
    InsufficientCredits,
    ReservationNotActive,
    SpendingLimitExceeded,
    WalletService,
)
from app.services.wallet import limits as spending
from app.services.wallet.queries import period_totals

logger = logging.getLogger("walk2ai.ai")

REASONING_EFFORTS = frozenset({"low", "medium", "high"})
COUNT_BASIS_PROVIDER = "provider_count"
COUNT_BASIS_LOCAL = "local_upper_bound"
_TURN_OVERHEAD = 8  # tokens of framing per message in the local upper bound
_REQUEST_OVERHEAD = 16
_NS_PER_MS = 1_000_000

# Failures that make counting pointless: the request itself would fail the same way.
_FATAL_COUNT_FAILURES = frozenset({FailureCode.AUTH, FailureCode.MODEL_NOT_FOUND})


class ProviderRequestFailed(AppError):
    status_code = 502
    code = "provider_error"
    title = "The AI provider could not be used"


class ConversationTooLong(ValidationFailed):
    code = "conversation_too_long"
    title = "The conversation is longer than this model accepts"


@dataclass(frozen=True)
class ChatCommand:
    request_id: uuid.UUID | None  # None for an estimate
    session_id: uuid.UUID | None
    model_id: uuid.UUID
    message: str
    max_output_tokens: int | None = None
    temperature: Decimal | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Plan:
    usable: catalogue.UsableModel
    request: ProviderRequest
    chat_session: ChatSession | None
    prompt_tokens: int
    prompt_tokens_basis: str
    max_output_tokens: int
    worst_case: Decimal  # price currency
    reserve_wc: int


@dataclass(frozen=True)
class Estimate:
    plan: Plan
    available_wc: int
    can_afford: bool
    limit_refusal: str | None  # which spending limit would refuse it, if any
    limit_detail: str | None


@dataclass
class ChatHandle:
    """A running request. ``start`` is known immediately; the rest arrives as events."""

    start: dict[str, Any]
    queue: asyncio.Queue[tuple[str, dict[str, Any]]]
    task: asyncio.Task[None]

    async def events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        while True:
            name, data = await self.queue.get()
            if name == "done":
                return
            yield name, data


@dataclass
class _Outcome:
    status: str  # succeeded | failed | timeout | cancelled | unknown
    usage: TokenUsage | None = None
    charge_worst_case: bool = False
    stop_reason: str | None = None
    provider_request_id: str | None = None
    served_model: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    text_parts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Context:
    user_id: uuid.UUID
    timezone: str
    request_id: uuid.UUID
    usage_id: uuid.UUID
    chat_id: uuid.UUID
    user_message_id: uuid.UUID
    plan: Plan
    stream: bool


def _local_upper_bound(request: ProviderRequest) -> int:
    """A prompt size that cannot be exceeded: byte-level tokenisers never produce more
    tokens than there are UTF-8 bytes, plus framing per message."""
    text_bytes = sum(len(turn.content.encode("utf-8")) for turn in request.turns)
    if request.system:
        text_bytes += len(request.system.encode("utf-8"))
    return text_bytes + _TURN_OVERHEAD * len(request.turns) + _REQUEST_OVERHEAD


def _money(value: Decimal) -> str:
    return format(stored(value), "f")


class AIGateway:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        clock: Clock,
        providers: Mapping[str, ProviderAdapter],
        inflight: set[asyncio.Task[None]],
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._clock = clock
        self._providers = providers
        self._inflight = inflight

    # ---------------------------------------------------------------- planning

    async def _plan(self, user: User, command: ChatCommand) -> Plan:
        async with self._sessions() as session:
            usable = await catalogue.resolve(
                session, command.model_id, self._providers.keys(), self._clock.now()
            )
            model = usable.model
            chat_session: ChatSession | None = None
            turns: list[Turn] = []
            if command.session_id is not None:
                chat_session = await chat.owned_session(session, user.id, command.session_id)
                turns = await chat.history(
                    session, chat_session.id, self._settings.ai_history_messages
                )

        max_output = self._max_output(model, command.max_output_tokens)
        self._check_options(model, command)
        request = ProviderRequest(
            model=model.model_identifier,
            turns=chat.with_new_message(turns, command.message),
            max_output_tokens=max_output,
            temperature=command.temperature,
            reasoning_effort=command.reasoning_effort,
        )

        adapter = self._providers[model.provider]
        try:
            prompt_tokens = await adapter.count_input_tokens(request)
            basis = COUNT_BASIS_PROVIDER
        except ProviderFailure as failure:
            if failure.code in _FATAL_COUNT_FAILURES:
                raise ProviderRequestFailed(
                    failure.user_message, extra={"provider_code": failure.code.value}
                ) from failure
            logger.warning(
                "token counting failed; using a local upper bound",
                extra={"provider": model.provider, "code": failure.code.value},
            )
            prompt_tokens = _local_upper_bound(request)
            basis = COUNT_BASIS_LOCAL

        if model.context_limit and prompt_tokens + max_output > model.context_limit:
            raise ConversationTooLong(
                f"This conversation needs about {prompt_tokens + max_output} tokens and "
                f"{model.display_name} accepts {model.context_limit}. Start a new conversation."
            )
        worst = worst_case_cost(prompt_tokens, max_output, usable.sheet)
        return Plan(
            usable=usable,
            request=request,
            chat_session=chat_session,
            prompt_tokens=prompt_tokens,
            prompt_tokens_basis=basis,
            max_output_tokens=max_output,
            worst_case=worst,
            reserve_wc=max(1, credits_for(worst, usable.conversion)),
        )

    def _max_output(self, model: AIModel, requested: int | None) -> int:
        cap = self._settings.ai_max_output_tokens
        if model.max_output_tokens:
            cap = min(cap, model.max_output_tokens)
        value = requested if requested is not None else self._settings.ai_default_max_output_tokens
        if value > cap:
            raise ValidationFailed(f"max_output_tokens may be at most {cap} for this model.")
        return min(value, cap)

    @staticmethod
    def _check_options(model: AIModel, command: ChatCommand) -> None:
        if command.temperature is not None and not model.supports_temperature:
            raise ValidationFailed(f"{model.display_name} does not accept a temperature.")
        if command.reasoning_effort is not None:
            if not model.supports_reasoning_effort:
                raise ValidationFailed(f"{model.display_name} does not accept a reasoning effort.")
            if command.reasoning_effort not in REASONING_EFFORTS:
                raise ValidationFailed("reasoning_effort must be low, medium or high.")

    # ---------------------------------------------------------------- estimate

    async def estimate(self, user: User, command: ChatCommand) -> Estimate:
        """What a message would cost at most, and whether it would be allowed. Writes nothing."""
        plan = await self._plan(user, command)
        async with self._sessions() as session:
            wallet = await WalletService(session, self._clock).get_wallet(user.id)
            refusal: str | None = None
            detail: str | None = None
            limits = await session.get(SpendingLimits, user.id)
            if limits is not None and spending.has_any_limit(limits):
                usage = await spending.spending_usage(
                    session, user.id, timezone=user.timezone, now=self._clock.now()
                )
                try:
                    spending.enforce_limits(
                        limits,
                        usage,
                        available=wallet.available_balance,
                        held=wallet.reserved_balance,
                        amount=plan.reserve_wc,
                    )
                except SpendingLimitExceeded as exceeded:
                    refusal = str(exceeded.extra.get("limit"))
                    detail = exceeded.detail
        return Estimate(
            plan=plan,
            available_wc=wallet.available_balance,
            can_afford=wallet.available_balance >= plan.reserve_wc,
            limit_refusal=refusal,
            limit_detail=detail,
        )

    # -------------------------------------------------------------------- chat

    async def start(self, user: User, command: ChatCommand, *, stream: bool) -> ChatHandle:
        """Plan, reserve and launch a request. Raises (402/409/422/502) before anything is
        sent to a provider if the request cannot be paid for or is not allowed."""
        if command.request_id is None:
            raise ValidationFailed("request_id is required.")
        plan = await self._plan(user, command)
        model = plan.usable.model
        now = self._clock.now()
        ttl = timedelta(
            seconds=self._settings.ai_request_timeout_seconds
            + self._settings.ai_reservation_margin_seconds
        )

        async with self._sessions() as session, session.begin():
            wallets = WalletService(session, self._clock)
            reservation = await wallets.reserve(
                user.id,
                plan.reserve_wc,
                request_id=command.request_id,
                ttl=ttl,
                description=f"AI request to {model.display_name}",
            )
            if plan.chat_session is None:
                chat_session = ChatSession(
                    user_id=user.id,
                    title=chat.title_from(command.message),
                    provider=model.provider,
                    model=model.model_identifier,
                    model_id=model.id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(chat_session)
                await session.flush()
            else:
                chat_session = await chat.owned_session(session, user.id, plan.chat_session.id)
            user_message = ChatMessage(
                session_id=chat_session.id,
                role=TurnRole.USER.value,
                content=command.message,
                status=chat.COMPLETE,
                created_at=now,
            )
            session.add(user_message)
            conversion = plan.usable.conversion
            usage = AIUsage(
                user_id=user.id,
                session_id=chat_session.id,
                request_id=command.request_id,
                reservation_id=reservation.id,
                model_id=model.id,
                pricing_id=plan.usable.price.id,
                provider=model.provider,
                model=model.model_identifier,
                estimated_cost=stored(plan.worst_case),
                provider_currency=plan.usable.sheet.currency,
                exchange_rate=conversion.to_eur,
                wc_per_eur=conversion.wc_per_eur,
                credits_reserved=plan.reserve_wc,
                status="pending",
                streamed=stream,
                created_at=now,
            )
            session.add(usage)
            await session.flush()
            context = _Context(
                user_id=user.id,
                timezone=user.timezone,
                request_id=command.request_id,
                usage_id=usage.id,
                chat_id=chat_session.id,
                user_message_id=user_message.id,
                plan=plan,
                stream=stream,
            )
            reserved_after = reservation.reserved_amount

        start = {
            "request_id": str(command.request_id),
            "session_id": str(context.chat_id),
            "provider": model.provider,
            "model": model.model_identifier,
            "reserved_wc": reserved_after,
            "estimate": {
                "input_tokens": plan.prompt_tokens,
                "input_tokens_basis": plan.prompt_tokens_basis,
                "max_output_tokens": plan.max_output_tokens,
                "worst_case_cost": _money(plan.worst_case),
                "currency": plan.usable.sheet.currency,
                "worst_case_eur": _money(to_eur(plan.worst_case, plan.usable.conversion)),
                "worst_case_wc": plan.reserve_wc,
            },
        }
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        task = asyncio.create_task(
            self._run(context, queue), name=f"ai-request-{command.request_id}"
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return ChatHandle(start=start, queue=queue, task=task)

    async def _run(
        self, context: _Context, queue: asyncio.Queue[tuple[str, dict[str, Any]]]
    ) -> None:
        adapter = self._providers[context.plan.usable.model.provider]
        outcome = _Outcome(status="unknown")
        started = False
        began = time.monotonic_ns()
        try:
            async with asyncio.timeout(self._settings.ai_request_timeout_seconds):
                async for event in adapter.stream(context.plan.request):
                    if isinstance(event, Started):
                        started = True
                        outcome.provider_request_id = event.provider_request_id
                    elif isinstance(event, TextDelta):
                        started = True
                        outcome.text_parts.append(event.text)
                        queue.put_nowait(("delta", {"text": event.text}))
                    elif isinstance(event, Completed):
                        outcome.status = "succeeded"
                        outcome.usage = event.usage
                        outcome.stop_reason = event.stop_reason
                        outcome.provider_request_id = (
                            event.provider_request_id or outcome.provider_request_id
                        )
                        outcome.served_model = event.served_model
            if outcome.usage is None:
                raise ProviderFailure(FailureCode.INCOMPLETE)
        except ProviderFailure as failure:
            self._failed(outcome, failure, started=started)
        except TimeoutError:
            self._failed(outcome, ProviderFailure(FailureCode.TIMEOUT), started=started)
        except asyncio.CancelledError:
            # Application shutdown. Settle conservatively, then let the cancellation finish.
            outcome.status = "cancelled"
            outcome.error_code = "server_shutdown"
            outcome.error_message = "The server stopped while the answer was being generated."
            outcome.charge_worst_case = started
            with anyio.CancelScope(shield=True):
                await self._settle(context, outcome, queue, began)
            raise
        except Exception:
            logger.exception("unexpected failure during an AI request")
            outcome.status = "unknown"
            outcome.error_code = "internal_error"
            outcome.error_message = "An unexpected error occurred."
            outcome.charge_worst_case = started
        with anyio.CancelScope(shield=True):
            await self._settle(context, outcome, queue, began)

    @staticmethod
    def _failed(outcome: _Outcome, failure: ProviderFailure, *, started: bool) -> None:
        outcome.error_code = failure.code.value
        outcome.error_message = failure.user_message
        if failure.billed_usage is not None and failure.billed_usage.billable:
            # The provider said what it used: that is what it billed, and what we charge.
            outcome.status = "failed"
            outcome.usage = failure.billed_usage
        elif started:
            # Generation had begun and the provider did not report what it used. Charge
            # the reserved worst case rather than risk real spend exceeding credits.
            outcome.status = "timeout" if failure.code is FailureCode.TIMEOUT else "failed"
            outcome.charge_worst_case = True
        else:
            # Refused before generating: not billed. A timeout here is "unknown" — the
            # provider may still have processed it — and is flagged for reconciliation.
            outcome.status = "unknown" if failure.code is FailureCode.TIMEOUT else "failed"

    async def _settle(
        self,
        context: _Context,
        outcome: _Outcome,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        began: int,
    ) -> None:
        plan = context.plan
        sheet, conversion = plan.usable.sheet, plan.usable.conversion
        text = "".join(outcome.text_parts)
        duration_ms = (time.monotonic_ns() - began) // _NS_PER_MS
        cost_basis = "none"
        provider_cost: Decimal | None = None
        credits = 0
        if outcome.usage is not None:
            charge = charge_for(outcome.usage, sheet, conversion)
            provider_cost, credits, cost_basis = charge.cost, charge.credits, "provider_usage"
        elif outcome.charge_worst_case:
            provider_cost, credits, cost_basis = plan.worst_case, plan.reserve_wc, "estimated"

        charged = released = shortfall = 0
        now = self._clock.now()
        try:
            async with self._sessions() as session, session.begin():
                wallets = WalletService(session, self._clock)
                usage_row = await session.get(AIUsage, context.usage_id, with_for_update=True)
                if usage_row is None:  # created with the reservation; never deleted
                    raise RuntimeError("usage record missing")
                try:
                    if credits > 0:
                        settlement = await wallets.settle(
                            context.request_id,
                            credits,
                            description=self._ledger_description(plan, outcome),
                            reference_id=usage_row.id,
                        )
                        charged, released = settlement.charged, settlement.released
                        shortfall = settlement.shortfall
                    else:
                        reservation = await wallets.release(
                            context.request_id, reason="AI request not billed"
                        )
                        released = reservation.released_amount or 0
                except ReservationNotActive:
                    # The hold expired before the request finished (it outlived its time
                    # limit and the sweeper released it). Nothing can be charged now.
                    logger.error(
                        "AI request finished after its reservation expired",
                        extra={"request_id": str(context.request_id)},
                    )
                    outcome.status = "unknown"
                    outcome.error_code = outcome.error_code or "reservation_expired"

                usage = outcome.usage
                usage_row.status = outcome.status
                usage_row.cost_basis = cost_basis
                usage_row.provider_cost = (
                    stored(provider_cost) if provider_cost is not None else None
                )
                usage_row.cost_eur = (
                    stored(to_eur(provider_cost, conversion)) if provider_cost is not None else None
                )
                usage_row.credits_charged = charged
                usage_row.stop_reason = outcome.stop_reason
                usage_row.provider_request_id = outcome.provider_request_id
                usage_row.error_code = outcome.error_code
                usage_row.error_message = outcome.error_message
                usage_row.duration_ms = duration_ms
                usage_row.completed_at = now
                if usage is not None:
                    usage_row.input_tokens = usage.input_tokens
                    usage_row.cached_tokens = usage.cache_read_tokens
                    usage_row.cache_write_tokens = (
                        usage.cache_write_tokens + usage.cache_write_1h_tokens
                    )
                    usage_row.output_tokens = usage.output_tokens
                    usage_row.reasoning_tokens = usage.reasoning_tokens

                # The conversation may have been deleted while the answer was being
                # generated. Lock it so it cannot vanish between this check and the
                # insert; without it, the charge is still recorded but the text is not.
                chat_row = await session.scalar(
                    select(ChatSession)
                    .where(ChatSession.id == context.chat_id)
                    .with_for_update(key_share=True)
                )
                answer_id: uuid.UUID | None = None
                if chat_row is not None and text:
                    answer = ChatMessage(
                        session_id=context.chat_id,
                        role=TurnRole.ASSISTANT.value,
                        content=text,
                        status=chat.COMPLETE if outcome.status == "succeeded" else chat.PARTIAL,
                        usage_id=usage_row.id,
                        created_at=now,
                    )
                    session.add(answer)
                    await session.flush()
                    answer_id = answer.id
                elif chat_row is not None and outcome.status != "succeeded":
                    question = await session.get(ChatMessage, context.user_message_id)
                    if question is not None:
                        question.status = chat.ERROR
                if chat_row is not None:
                    chat_row.updated_at = now
                    chat_row.provider = plan.usable.model.provider
                    chat_row.model = plan.usable.model.model_identifier
                    chat_row.model_id = plan.usable.model.id

            wallet_view, spending_view = await self._balances(context)
        except Exception:
            logger.exception(
                "failed to settle an AI request", extra={"request_id": str(context.request_id)}
            )
            queue.put_nowait(
                (
                    "error",
                    {
                        "request_id": str(context.request_id),
                        "code": "settlement_failed",
                        "detail": "The answer could not be recorded. Its credits remain held "
                        "until they are released automatically.",
                    },
                )
            )
            queue.put_nowait(("done", {}))
            return

        usage = outcome.usage
        if outcome.status == "succeeded" and usage is not None:
            queue.put_nowait(
                (
                    "usage",
                    {
                        "request_id": str(context.request_id),
                        "session_id": str(context.chat_id),
                        "message_id": str(answer_id) if answer_id else None,
                        "status": outcome.status,
                        "stop_reason": outcome.stop_reason,
                        "provider": plan.usable.model.provider,
                        "model": plan.usable.model.model_identifier,
                        "tokens": {
                            "input": usage.input_tokens,
                            "cached_input": usage.cache_read_tokens,
                            "cache_write": usage.cache_write_tokens + usage.cache_write_1h_tokens,
                            "output": usage.output_tokens,
                            "reasoning": usage.reasoning_tokens,
                        },
                        "provider_cost": _money(provider_cost or Decimal(0)),
                        "currency": sheet.currency,
                        "cost_eur": _money(to_eur(provider_cost or Decimal(0), conversion)),
                        "charged_wc": charged,
                        "released_wc": released,
                        "shortfall_wc": shortfall,
                        "cost_basis": cost_basis,
                        "duration_ms": duration_ms,
                        "wallet": wallet_view,
                        "spending": spending_view,
                    },
                )
            )
        else:
            queue.put_nowait(
                (
                    "error",
                    {
                        "request_id": str(context.request_id),
                        "session_id": str(context.chat_id),
                        "message_id": str(answer_id) if answer_id else None,
                        "status": outcome.status,
                        "code": outcome.error_code,
                        "detail": outcome.error_message,
                        "charged_wc": charged,
                        "released_wc": released,
                        "cost_basis": cost_basis,
                        "wallet": wallet_view,
                        "spending": spending_view,
                    },
                )
            )
        queue.put_nowait(("done", {}))

    @staticmethod
    def _ledger_description(plan: Plan, outcome: _Outcome) -> str:
        name = plan.usable.model.display_name
        if outcome.usage is not None:
            usage = outcome.usage
            return f"AI: {name} ({usage.prompt_tokens} in / {usage.output_tokens} out tokens)"
        return f"AI: {name} (charged the reserved worst case: usage was not reported)"

    async def _balances(self, context: _Context) -> tuple[dict[str, int], dict[str, Any]]:
        async with self._sessions() as session:
            wallet = await session.scalar(
                select(WalletAccount).where(WalletAccount.user_id == context.user_id)
            )
            limits = await session.get(SpendingLimits, context.user_id)
            now = self._clock.now()
            today = local_date(now, context.timezone)
            day_start, day_end = day_bounds_utc(today, context.timezone)
            month_start, month_end = month_bounds_utc(today.year, today.month, context.timezone)
            day = await period_totals(session, context.user_id, start=day_start, end=day_end)
            month = await period_totals(session, context.user_id, start=month_start, end=month_end)
        if wallet is None:
            raise RuntimeError("wallet missing")
        threshold = limits.alert_threshold_pct if limits else 80
        held = wallet.reserved_balance
        daily = spending.limit_status(
            limits.max_daily_wc if limits else None, day.spent, held, threshold
        )
        monthly = spending.limit_status(
            limits.max_monthly_wc if limits else None, month.spent, held, threshold
        )
        return (
            {"available_wc": wallet.available_balance, "reserved_wc": wallet.reserved_balance},
            {
                "daily": {
                    "limit_wc": daily.limit,
                    "used_wc": daily.used,
                    "remaining_wc": daily.remaining,
                    "alert": daily.alert,
                },
                "monthly": {
                    "limit_wc": monthly.limit,
                    "used_wc": monthly.used,
                    "remaining_wc": monthly.remaining,
                    "alert": monthly.alert,
                },
            },
        )


async def wait_for_inflight(inflight: set[asyncio.Task[None]], timeout_seconds: int) -> None:
    """On shutdown: let running requests finish and settle; cancel what remains."""
    if not inflight:
        return
    pending = set(inflight)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*pending, return_exceptions=True)
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "AIGateway",
    "ChatCommand",
    "ChatHandle",
    "ConversationTooLong",
    "Estimate",
    "InsufficientCredits",
    "ProviderRequestFailed",
    "wait_for_inflight",
]
