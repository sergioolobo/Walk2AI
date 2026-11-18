"""AI endpoints: models, estimates, chat (streamed or not) and conversations."""

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    ClockDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
)
from app.core.errors import NotFound
from app.core.ratelimit import enforce_rate_limit
from app.models.ai import AIModel, AIUsage, ChatMessage, ChatSession, ModelPricing
from app.schemas.ai import (
    ChatRequest,
    ChatResult,
    EstimateRequest,
    EstimateResponse,
    MessageResponse,
    MessageUsage,
    ModelResponse,
    PricesResponse,
    ProviderResponse,
    SessionCreate,
    SessionDetail,
    SessionResponse,
    SessionUpdate,
    WorstCase,
)
from app.services.ai import catalogue, chat
from app.services.ai.gateway import AIGateway, ChatCommand
from app.services.ai.pricing import stored, to_eur
from app.services.ai.providers import KNOWN_PROVIDERS

router = APIRouter(prefix="/ai", tags=["ai"])


def get_gateway(request: Request) -> AIGateway:
    gateway: AIGateway = request.app.state.ai_gateway
    return gateway


GatewayDep = Annotated[AIGateway, Depends(get_gateway)]


def configured_providers(request: Request) -> set[str]:
    return set(request.app.state.ai_providers)


def _prices(price: ModelPricing) -> PricesResponse:
    return PricesResponse(
        currency=price.currency,
        input=price.input_price,
        output=price.output_price,
        cached_input=price.cached_input_price,
        cache_write=price.cache_write_price,
        cache_write_1h=price.cache_write_1h_price,
        long_context_threshold=price.long_context_threshold,
        long_context_input=price.long_context_input_price,
        long_context_output=price.long_context_output_price,
        verified=price.verified,
        verified_at=price.verified_at,
        source_url=price.source_url,
    )


@router.get("/providers", response_model=list[ProviderResponse])
async def providers(request: Request, _: PrincipalDep) -> list[ProviderResponse]:
    """Which providers this instance can call. Keys are never shown, only their presence."""
    configured = configured_providers(request)
    return [ProviderResponse(provider=p, configured=p in configured) for p in KNOWN_PROVIDERS]


@router.get("/models", response_model=list[ModelResponse])
async def models(
    request: Request,
    _: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> list[ModelResponse]:
    """Models that can be used right now, with their verified prices per million tokens."""
    statuses = await catalogue.list_statuses(session, configured_providers(request), clock.now())
    result = []
    for status_ in statuses:
        if not status_.usable or status_.price is None:
            continue
        model: AIModel = status_.model
        cap = settings.ai_max_output_tokens
        if model.max_output_tokens:
            cap = min(cap, model.max_output_tokens)
        result.append(
            ModelResponse(
                id=model.id,
                provider=model.provider,
                model=model.model_identifier,
                display_name=model.display_name,
                context_limit=model.context_limit,
                max_output_tokens=cap,
                default_max_output_tokens=min(settings.ai_default_max_output_tokens, cap),
                supports_temperature=model.supports_temperature,
                supports_reasoning_effort=model.supports_reasoning_effort,
                prices=_prices(status_.price),
            )
        )
    return result


@router.post("/estimate", response_model=EstimateResponse)
async def estimate(
    body: EstimateRequest, request: Request, principal: PrincipalDep, gateway: GatewayDep
) -> EstimateResponse:
    """The most a message can cost, and whether it would be allowed. Nothing is reserved."""
    enforce_rate_limit(
        request, "ai-estimate", limit=60, window_seconds=60, subject=str(principal.user.id)
    )
    result = await gateway.estimate(
        principal.user,
        ChatCommand(
            request_id=None,
            session_id=body.session_id,
            model_id=body.model_id,
            message=body.message,
            max_output_tokens=body.max_output_tokens,
            reasoning_effort=body.reasoning_effort,
        ),
    )
    plan = result.plan
    return EstimateResponse(
        input_tokens=plan.prompt_tokens,
        input_tokens_basis=plan.prompt_tokens_basis,
        max_output_tokens=plan.max_output_tokens,
        worst_case=WorstCase(
            cost=stored(plan.worst_case),
            currency=plan.usable.sheet.currency,
            cost_eur=stored(to_eur(plan.worst_case, plan.usable.conversion)),
            credits_wc=plan.reserve_wc,
        ),
        available_wc=result.available_wc,
        can_afford=result.can_afford,
        limit_refusal=result.limit_refusal,
        limit_detail=result.limit_detail,
    )


def _sse(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.post(
    "/chat",
    response_model=ChatResult,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Streamed events"}},
)
async def chat_endpoint(
    body: ChatRequest, request: Request, principal: PrincipalDep, gateway: GatewayDep
) -> Any:
    """Send a message. Credits for the worst case are reserved first; the actual cost is
    charged when the answer is complete.

    With ``stream: true`` (default) the response is ``text/event-stream`` with events
    ``start``, ``delta`` (text), then ``usage`` or ``error``, then ``done``. If the
    connection drops, the request still completes on the server and is charged exactly;
    the answer appears in the conversation.

    Refusals (insufficient credits, a spending limit, an unavailable model) are plain
    JSON errors and nothing is sent to the provider.
    """
    enforce_rate_limit(
        request, "ai-chat", limit=30, window_seconds=60, subject=str(principal.user.id)
    )
    handle = await gateway.start(
        principal.user,
        ChatCommand(
            request_id=body.request_id,
            session_id=body.session_id,
            model_id=body.model_id,
            message=body.message,
            max_output_tokens=body.max_output_tokens,
            temperature=body.temperature,
            reasoning_effort=body.reasoning_effort,
        ),
        stream=body.stream,
    )

    if body.stream:

        async def events() -> AsyncIterator[str]:
            yield _sse("start", handle.start)
            async for name, data in handle.events():
                yield _sse(name, data)
            yield _sse("done", {})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    text: list[str] = []
    outcome, result = "error", {}
    async for name, data in handle.events():
        if name == "delta":
            text.append(data["text"])
        else:
            outcome, result = name, data
    return ChatResult(start=handle.start, text="".join(text), outcome=outcome, result=result)


# ----------------------------------------------------------------- conversations


def _session_response(summary: chat.SessionSummary) -> SessionResponse:
    c = summary.chat
    return SessionResponse(
        id=c.id,
        title=c.title,
        provider=c.provider,
        model=c.model,
        model_id=c.model_id,
        created_at=c.created_at,
        updated_at=c.updated_at,
        messages=summary.messages,
        credits_charged_wc=summary.credits_charged,
    )


def _message_response(message: ChatMessage, usage: AIUsage | None) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        status=message.status,
        created_at=message.created_at,
        usage=(
            MessageUsage(
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                provider_cost=usage.provider_cost,
                currency=usage.provider_currency,
                cost_eur=usage.cost_eur,
                credits_charged_wc=usage.credits_charged,
                cost_basis=usage.cost_basis,
                status=usage.status,
                stop_reason=usage.stop_reason,
            )
            if usage is not None
            else None
        ),
    )


def _summarise(
    chat_row: ChatSession, rows: Sequence[tuple[ChatMessage, AIUsage | None]]
) -> chat.SessionSummary:
    return chat.SessionSummary(
        chat=chat_row,
        messages=len(rows),
        credits_charged=sum(u.credits_charged for _, u in rows if u is not None),
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SessionResponse]:
    """Conversations, most recently active first."""
    summaries = await chat.list_sessions(session, principal.user.id, limit=limit, offset=offset)
    return [_session_response(s) for s in summaries]


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate, principal: PrincipalDep, session: SessionDep, clock: ClockDep
) -> SessionResponse:
    model = await session.get(AIModel, body.model_id)
    if model is None:
        raise NotFound("Model not found.")
    now = clock.now()
    row = ChatSession(
        user_id=principal.user.id,
        title=body.title or "New conversation",
        provider=model.provider,
        model=model.model_identifier,
        model_id=model.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return _session_response(chat.SessionSummary(chat=row, messages=0, credits_charged=0))


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> SessionDetail:
    """A conversation with every message, and the tokens, cost and credits of each answer."""
    chat_row = await chat.owned_session(session, principal.user.id, session_id)
    rows = await chat.messages_with_usage(session, chat_row.id)
    summary = _summarise(chat_row, rows)
    return SessionDetail(
        **_session_response(summary).model_dump(),
        items=[_message_response(m, u) for m, u in rows],
    )


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    principal: PrincipalDep,
    session: SessionDep,
    clock: ClockDep,
) -> SessionResponse:
    chat_row = await chat.owned_session(session, principal.user.id, session_id)
    if body.title is not None:
        chat_row.title = body.title
    if body.model_id is not None:
        model = await session.get(AIModel, body.model_id)
        if model is None:
            raise NotFound("Model not found.")
        chat_row.provider = model.provider
        chat_row.model = model.model_identifier
        chat_row.model_id = model.id
    chat_row.updated_at = clock.now()
    summary = _summarise(chat_row, await chat.messages_with_usage(session, chat_row.id))
    await session.commit()
    return _session_response(summary)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> Response:
    """Delete a conversation and its messages. The cost records of its requests remain
    (without any content), so the wallet's history stays explainable."""
    chat_row = await chat.owned_session(session, principal.user.id, session_id)
    await session.delete(chat_row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
