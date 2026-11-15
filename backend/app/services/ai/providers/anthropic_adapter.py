"""Anthropic, through the official ``anthropic`` SDK and the Messages API.

Notes on the API this targets (SDK 2.x, API version 2026-10-21):
  * sampling parameters (``temperature``) are no longer accepted, so they are never sent;
  * current models think adaptively by default; thinking tokens are part of
    ``output_tokens`` and bounded by ``max_tokens``, so the gateway's worst case holds;
  * usage reports uncached input, cache reads and cache writes separately;
  * no prompt caching is requested yet, so cache categories are normally zero.
"""

from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2

from app.services.ai.pricing import TokenUsage
from app.services.ai.providers.base import (
    Completed,
    FailureCode,
    ModelCheck,
    ProviderEvent,
    ProviderFailure,
    ProviderRequest,
    Started,
    TextDelta,
)

PROVIDER = "anthropic"
MAX_RETRIES = 2


def _usage(start: Any, final: Any) -> TokenUsage:
    """Usage from the ``message_start`` usage and the final ``message_delta`` usage.

    The delta carries the cumulative output count and, when present, final input
    figures; anything it does not carry comes from the start event.
    """

    def pick(name: str) -> int:
        for source in (final, start):
            value = getattr(source, name, None) if source is not None else None
            if value is not None:
                return int(value)
        return 0

    cache_write_5m = 0
    cache_write_1h = 0
    breakdown = getattr(start, "cache_creation", None) if start is not None else None
    if breakdown is not None:
        cache_write_5m = int(breakdown.ephemeral_5m_input_tokens or 0)
        cache_write_1h = int(breakdown.ephemeral_1h_input_tokens or 0)
    total_cache_write = pick("cache_creation_input_tokens")
    if cache_write_5m + cache_write_1h != total_cache_write:
        # No (or inconsistent) breakdown: report every write as short-lived.
        cache_write_5m, cache_write_1h = total_cache_write, 0

    output = pick("output_tokens")
    reasoning = 0
    for source in (final, start):
        details = getattr(source, "output_tokens_details", None) if source is not None else None
        if details is not None and details.thinking_tokens is not None:
            reasoning = int(details.thinking_tokens)
            break
    return TokenUsage(
        input_tokens=pick("input_tokens"),
        cache_read_tokens=pick("cache_read_input_tokens"),
        cache_write_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        output_tokens=output,
        reasoning_tokens=min(reasoning, output),
    )


# Error types an ``error`` event can carry in the middle of a stream (the HTTP status is
# 200 by then, so the type in the body is the only signal).
_STREAM_ERROR_TYPES = {
    "overloaded_error": FailureCode.UNAVAILABLE,
    "api_error": FailureCode.UNAVAILABLE,
    "rate_limit_error": FailureCode.RATE_LIMITED,
    "invalid_request_error": FailureCode.REJECTED,
    "authentication_error": FailureCode.AUTH,
    "permission_error": FailureCode.AUTH,
    "not_found_error": FailureCode.MODEL_NOT_FOUND,
}


def _error_type(error: Exception) -> str | None:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("type"), str):
            return str(inner["type"])
    return None


def _failure(error: Exception) -> ProviderFailure:
    """Map SDK exceptions to the gateway's vocabulary. Most specific first."""
    status = getattr(error, "status_code", None)
    message = getattr(error, "message", None) or str(error)
    if isinstance(error, anthropic.APIStatusError) and error.status_code < 400:
        code = _STREAM_ERROR_TYPES.get(_error_type(error) or "", FailureCode.ERROR)
    elif isinstance(error, anthropic.EnforcedSpendLimitReachedError):
        code = FailureCode.SPEND_LIMIT
    elif isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        code = FailureCode.AUTH
    elif isinstance(error, anthropic.NotFoundError):
        code = FailureCode.MODEL_NOT_FOUND
    elif isinstance(error, anthropic.RateLimitError):
        code = FailureCode.RATE_LIMITED
    elif isinstance(error, anthropic.APITimeoutError):
        code = FailureCode.TIMEOUT
    elif isinstance(error, anthropic.APIConnectionError):
        code = FailureCode.UNAVAILABLE
    elif isinstance(error, anthropic.APIStatusError):
        if error.status_code >= 500:
            code = FailureCode.UNAVAILABLE
        elif error.status_code in (400, 413, 422):
            code = FailureCode.REJECTED
        else:
            code = FailureCode.ERROR
    else:
        code = FailureCode.ERROR
    return ProviderFailure(code, message, status_code=status)


class AnthropicAdapter:
    provider = PROVIDER

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }
        if base_url:
            options["base_url"] = base_url
        if http_client is not None:
            options["http_client"] = http_client
        self._client = anthropic.AsyncAnthropic(**options)

    @staticmethod
    def _messages(request: ProviderRequest) -> list[dict[str, str]]:
        return [{"role": turn.role.value, "content": turn.content} for turn in request.turns]

    def _params(self, request: ProviderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": self._messages(request),
        }
        if request.system:
            params["system"] = request.system
        if request.reasoning_effort:
            params["output_config"] = {"effort": request.reasoning_effort}
        return params

    async def count_input_tokens(self, request: ProviderRequest) -> int:
        params: dict[str, Any] = {"model": request.model, "messages": self._messages(request)}
        if request.system:
            params["system"] = request.system
        try:
            result = await self._client.messages.count_tokens(**params)
        except anthropic.AnthropicError as error:
            raise _failure(error) from error
        return int(result.input_tokens)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        start_usage: Any = None
        final_usage: Any = None
        stop_reason: str | None = None
        message_id: str | None = None
        served_model: str | None = None
        finished = False
        try:
            events = await self._client.messages.create(**self._params(request), stream=True)
            async with events:
                async for event in events:
                    if event.type == "message_start":
                        message_id = event.message.id
                        served_model = event.message.model
                        start_usage = event.message.usage
                        yield Started(provider_request_id=message_id)
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        if event.delta.text:
                            yield TextDelta(event.delta.text)
                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason or stop_reason
                        final_usage = event.usage
                    elif event.type == "message_stop":
                        finished = True
        except anthropic.AnthropicError as error:
            failure = _failure(error)
            if start_usage is not None and final_usage is not None:
                failure.billed_usage = _usage(start_usage, final_usage)
            raise failure from error

        if not finished or start_usage is None:
            raise ProviderFailure(FailureCode.INCOMPLETE)
        yield Completed(
            usage=_usage(start_usage, final_usage),
            stop_reason=stop_reason,
            provider_request_id=message_id,
            served_model=served_model,
        )

    async def check_model(self, model: str) -> ModelCheck:
        try:
            info = await self._client.models.retrieve(model)
        except anthropic.NotFoundError:
            return ModelCheck(available=False, detail="Not available to this API account.")
        except anthropic.AnthropicError as error:
            raise _failure(error) from error
        return ModelCheck(
            available=True,
            context_limit=info.max_input_tokens,
            max_output_tokens=info.max_tokens,
        )

    async def aclose(self) -> None:
        await self._client.close()
