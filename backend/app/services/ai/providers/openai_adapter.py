"""OpenAI, through the official ``openai`` SDK and the Responses API.

Notes:
  * ``store=False``: conversations are kept in Walk2AI's own database, not at OpenAI;
  * ``max_output_tokens`` bounds visible output and reasoning together;
  * usage reports *total* input with cached and cache-write portions inside it, so the
    uncached input is the remainder;
  * a 429 with ``insufficient_quota`` means the account's budget or credit is exhausted,
    not a temporary rate limit.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import openai

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

PROVIDER = "openai"
MAX_RETRIES = 2
_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


def _usage(usage: Any) -> TokenUsage:
    total_input = int(usage.input_tokens or 0)
    details = usage.input_tokens_details
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    written = int(getattr(details, "cache_write_tokens", 0) or 0) if details is not None else 0
    output = int(usage.output_tokens or 0)
    out_details = usage.output_tokens_details
    reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0
    return TokenUsage(
        input_tokens=max(0, total_input - cached - written),
        cache_read_tokens=cached,
        cache_write_tokens=written,
        output_tokens=output,
        reasoning_tokens=min(reasoning, output),
    )


def _error_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and inner.get("code"):
            return str(inner["code"])
    return None


def _failure(error: Exception) -> ProviderFailure:
    status = getattr(error, "status_code", None)
    message = getattr(error, "message", None) or str(error)
    if isinstance(error, openai.RateLimitError) and _error_code(error) in _QUOTA_CODES:
        code = FailureCode.SPEND_LIMIT
    elif isinstance(error, openai.AuthenticationError | openai.PermissionDeniedError):
        code = FailureCode.AUTH
    elif isinstance(error, openai.NotFoundError):
        code = FailureCode.MODEL_NOT_FOUND
    elif isinstance(error, openai.RateLimitError):
        code = FailureCode.RATE_LIMITED
    elif isinstance(error, openai.APITimeoutError):
        code = FailureCode.TIMEOUT
    elif isinstance(error, openai.APIConnectionError):
        code = FailureCode.UNAVAILABLE
    elif isinstance(error, openai.APIStatusError):
        if error.status_code >= 500:
            code = FailureCode.UNAVAILABLE
        elif error.status_code in (400, 413, 422):
            code = FailureCode.REJECTED
        else:
            code = FailureCode.ERROR
    else:
        code = FailureCode.ERROR
    return ProviderFailure(code, message, status_code=status)


class OpenAIAdapter:
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
        self._client = openai.AsyncOpenAI(**options)

    @staticmethod
    def _input(request: ProviderRequest) -> list[dict[str, str]]:
        return [{"role": turn.role.value, "content": turn.content} for turn in request.turns]

    def _params(self, request: ProviderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "input": self._input(request),
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.system:
            params["instructions"] = request.system
        if request.temperature is not None:
            params["temperature"] = float(request.temperature)  # the API's own type
        if request.reasoning_effort:
            params["reasoning"] = {"effort": request.reasoning_effort}
        return params

    async def count_input_tokens(self, request: ProviderRequest) -> int:
        params: dict[str, Any] = {"model": request.model, "input": self._input(request)}
        if request.system:
            params["instructions"] = request.system
        try:
            result = await self._client.responses.input_tokens.count(**params)
        except openai.OpenAIError as error:
            raise _failure(error) from error
        return int(result.input_tokens)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        completed: Completed | None = None
        try:
            events = await self._client.responses.create(**self._params(request), stream=True)
            async with events:
                async for event in events:
                    if event.type == "response.created":
                        yield Started(provider_request_id=event.response.id)
                    elif event.type == "response.output_text.delta":
                        if event.delta:
                            yield TextDelta(event.delta)
                    elif event.type in ("response.completed", "response.incomplete"):
                        response = event.response
                        if response.usage is None:
                            raise ProviderFailure(FailureCode.INCOMPLETE)
                        reason = "end_turn"
                        if event.type == "response.incomplete":
                            details = response.incomplete_details
                            reason = (details.reason if details else None) or "incomplete"
                        completed = Completed(
                            usage=_usage(response.usage),
                            stop_reason=reason,
                            provider_request_id=response.id,
                            served_model=response.model,
                        )
                    elif event.type == "response.failed":
                        response = event.response
                        message = response.error.message if response.error else None
                        billed = _usage(response.usage) if response.usage else None
                        raise ProviderFailure(FailureCode.FAILED, message, billed_usage=billed)
                    elif event.type == "error":
                        raise ProviderFailure(FailureCode.ERROR, event.message)
        except openai.OpenAIError as error:
            raise _failure(error) from error

        if completed is None:
            raise ProviderFailure(FailureCode.INCOMPLETE)
        yield completed

    async def check_model(self, model: str) -> ModelCheck:
        try:
            await self._client.models.retrieve(model)
        except openai.NotFoundError:
            return ModelCheck(available=False, detail="Not available to this API account.")
        except openai.OpenAIError as error:
            raise _failure(error) from error
        # OpenAI's models endpoint does not report context or output limits.
        return ModelCheck(available=True)

    async def aclose(self) -> None:
        await self._client.close()
