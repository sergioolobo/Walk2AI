"""Provider adapters, exercised through the real official SDKs.

The SDK clients talk to an in-memory transport that answers with responses in the shape
the real APIs send. So the SDKs' own request building, stream parsing and error classes
all run for real — only the network is replaced. No real API call is made.
"""

from collections.abc import Callable
from decimal import Decimal

import httpx2
import pytest

from app.services.ai.pricing import TokenUsage
from app.services.ai.providers import (
    AnthropicAdapter,
    Completed,
    FailureCode,
    OpenAIAdapter,
    ProviderAdapter,
    ProviderFailure,
    ProviderRequest,
    Started,
    TextDelta,
    Turn,
    TurnRole,
)
from tests.provider_fixtures import (
    RecordingProvider,
    anthropic_error,
    anthropic_model,
    anthropic_stream,
    openai_error,
    openai_stream,
    openai_usage,
    stream_response,
)

pytestmark = pytest.mark.anyio

Handler = Callable[[httpx2.Request], httpx2.Response]

REQUEST = ProviderRequest(
    model="claude-haiku-5-5",
    turns=(
        Turn(TurnRole.USER, "Olá! Quantos passos são 5 km?"),
        Turn(TurnRole.ASSISTANT, "Cerca de 6 500 passos."),
        Turn(TurnRole.USER, "E 10 km?"),
    ),
    max_output_tokens=1024,
    system="Answer briefly.",
)


def anthropic(handler: Handler) -> tuple[AnthropicAdapter, RecordingProvider]:
    provider = RecordingProvider(handler)
    adapter = AnthropicAdapter(
        "sk-ant-test", timeout_seconds=30, http_client=provider.client(), max_retries=0
    )
    return adapter, provider


def openai_adapter(handler: Handler) -> tuple[OpenAIAdapter, RecordingProvider]:
    provider = RecordingProvider(handler)
    adapter = OpenAIAdapter(
        "sk-test", timeout_seconds=30, http_client=provider.client(), max_retries=0
    )
    return adapter, provider


async def collect(adapter: ProviderAdapter, request: ProviderRequest) -> list[object]:
    return [event async for event in adapter.stream(request)]


# ------------------------------------------------------------------- Anthropic


async def test_anthropic_streams_text_and_reports_exact_usage() -> None:
    adapter, _ = anthropic(
        lambda _: stream_response(
            anthropic_stream(
                ["Cerca de ", "13 000 ", "passos."], output_tokens=140, thinking_tokens=90
            )
        )
    )
    events = await collect(adapter, REQUEST)

    assert events[0] == Started(provider_request_id="msg_01TEST")
    assert [e.text for e in events if isinstance(e, TextDelta)] == [
        "Cerca de ",
        "13 000 ",
        "passos.",
    ]
    final = events[-1]
    assert isinstance(final, Completed)
    assert final.usage == TokenUsage(input_tokens=25, output_tokens=140, reasoning_tokens=90)
    assert (final.stop_reason, final.served_model) == ("end_turn", "claude-haiku-5-5")
    await adapter.aclose()


async def test_anthropic_request_has_the_expected_shape() -> None:
    adapter, provider = anthropic(lambda _: stream_response(anthropic_stream(["ok"])))
    await collect(adapter, ProviderRequest(**{**REQUEST.__dict__, "reasoning_effort": "low"}))

    sent = provider.requests[0]
    body = provider.body()
    assert sent.url.path == "/v1/messages"
    assert sent.headers["authorization"] == "Bearer sk-ant-test"  # SDK 2.x: bearer auth
    assert sent.headers["anthropic-version"] == "2026-10-21"
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 1024
    assert body["stream"] is True
    assert body["system"] == "Answer briefly."
    assert body["messages"] == [
        {"role": "user", "content": "Olá! Quantos passos são 5 km?"},
        {"role": "assistant", "content": "Cerca de 6 500 passos."},
        {"role": "user", "content": "E 10 km?"},
    ]
    assert body["output_config"] == {"effort": "low"}
    # Sampling parameters are not accepted by this API version and are never sent.
    assert "temperature" not in body


async def test_anthropic_reports_cache_categories_separately() -> None:
    adapter, _ = anthropic(
        lambda _: stream_response(
            anthropic_stream(
                ["x"], input_tokens=50, cache_read=4000, cache_write_5m=300, cache_write_1h=200
            )
        )
    )
    final = (await collect(adapter, REQUEST))[-1]
    assert isinstance(final, Completed)
    assert final.usage == TokenUsage(
        input_tokens=50,
        cache_read_tokens=4000,
        cache_write_tokens=300,
        cache_write_1h_tokens=200,
        output_tokens=120,
        reasoning_tokens=80,
    )


async def test_anthropic_refusal_and_length_stops_are_completions_with_usage() -> None:
    for reason in ("refusal", "max_tokens"):
        events = anthropic_stream(["Partial"], stop_reason=reason)
        adapter, _ = anthropic(lambda _, e=events: stream_response(e))  # type: ignore[misc]
        final = (await collect(adapter, REQUEST))[-1]
        assert isinstance(final, Completed)
        assert final.stop_reason == reason
        assert final.usage.output_tokens == 120  # billed, so reported


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (anthropic_error(401, "authentication_error", "invalid x-api-key"), FailureCode.AUTH),
        (anthropic_error(403, "permission_error", "not allowed"), FailureCode.AUTH),
        (anthropic_error(404, "not_found_error", "model: nope"), FailureCode.MODEL_NOT_FOUND),
        (anthropic_error(429, "rate_limit_error", "slow down"), FailureCode.RATE_LIMITED),
        (
            anthropic_error(
                429, "rate_limit_error", "spend limit reached", code="enforced_spend_limit_reached"
            ),
            FailureCode.SPEND_LIMIT,
        ),
        (anthropic_error(400, "invalid_request_error", "bad"), FailureCode.REJECTED),
        (anthropic_error(500, "api_error", "oops"), FailureCode.UNAVAILABLE),
        (anthropic_error(529, "overloaded_error", "Overloaded"), FailureCode.UNAVAILABLE),
    ],
)
async def test_anthropic_http_errors_are_mapped(
    response: httpx2.Response, code: FailureCode
) -> None:
    adapter, _ = anthropic(lambda _: response)
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, REQUEST)
    assert excinfo.value.code is code
    assert excinfo.value.billed_usage is None  # an error response is not billed


async def test_anthropic_error_in_the_middle_of_a_stream() -> None:
    adapter, _ = anthropic(
        lambda _: stream_response(
            anthropic_stream(
                ["Começa"], error={"type": "overloaded_error", "message": "Overloaded"}
            )
        )
    )
    received: list[object] = []
    with pytest.raises(ProviderFailure) as excinfo:
        async for event in adapter.stream(REQUEST):
            received.append(event)
    assert excinfo.value.code is FailureCode.UNAVAILABLE
    assert [e.text for e in received if isinstance(e, TextDelta)] == ["Começa"]


async def test_anthropic_stream_that_just_stops_is_incomplete() -> None:
    adapter, _ = anthropic(lambda _: stream_response(anthropic_stream(["Começa"], finish=False)))
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, REQUEST)
    assert excinfo.value.code is FailureCode.INCOMPLETE


async def test_anthropic_connection_failure() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    adapter, _ = anthropic(refuse)
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, REQUEST)
    assert excinfo.value.code is FailureCode.UNAVAILABLE


async def test_anthropic_timeout() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    adapter, _ = anthropic(slow)
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, REQUEST)
    assert excinfo.value.code is FailureCode.TIMEOUT


async def test_anthropic_counts_input_tokens_and_checks_models() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 57})
        if request.url.path == "/v1/models/claude-haiku-5-5":
            return anthropic_model()
        return anthropic_error(404, "not_found_error", "no such model")

    adapter, provider = anthropic(handler)
    assert await adapter.count_input_tokens(REQUEST) == 57
    assert provider.body()["system"] == "Answer briefly."
    check = await adapter.check_model("claude-haiku-5-5")
    assert (check.available, check.context_limit, check.max_output_tokens) == (
        True,
        1_000_000,
        128_000,
    )
    missing = await adapter.check_model("claude-nonexistent")
    assert missing.available is False


# ---------------------------------------------------------------------- OpenAI

OPENAI_REQUEST = ProviderRequest(**{**REQUEST.__dict__, "model": "gpt-6-luna"})


async def test_openai_streams_text_and_derives_uncached_input() -> None:
    adapter, _ = openai_adapter(
        lambda _: stream_response(
            openai_stream(
                ["Cerca de ", "13 000 passos."],
                usage=openai_usage(1200, 300, cached=1000, cache_write=50, reasoning=120),
            )
        )
    )
    events = await collect(adapter, OPENAI_REQUEST)

    assert events[0] == Started(provider_request_id="resp_TEST")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Cerca de 13 000 passos."
    final = events[-1]
    assert isinstance(final, Completed)
    # OpenAI reports total input; cached and cache-write tokens are inside it.
    assert final.usage == TokenUsage(
        input_tokens=150,
        cache_read_tokens=1000,
        cache_write_tokens=50,
        output_tokens=300,
        reasoning_tokens=120,
    )
    assert (final.stop_reason, final.served_model) == ("end_turn", "gpt-6-luna")


async def test_openai_request_has_the_expected_shape() -> None:
    adapter, provider = openai_adapter(lambda _: stream_response(openai_stream(["ok"])))
    await collect(
        adapter,
        ProviderRequest(
            **{**OPENAI_REQUEST.__dict__, "temperature": Decimal("0.3"), "reasoning_effort": "low"}
        ),
    )
    sent = provider.requests[0]
    body = provider.body()
    assert sent.url.path == "/v1/responses"
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "gpt-6-luna"
    assert body["max_output_tokens"] == 1024
    assert body["instructions"] == "Answer briefly."
    assert body["store"] is False  # conversations stay in Walk2AI's own database
    assert body["stream"] is True
    assert body["temperature"] == 0.3
    assert body["reasoning"] == {"effort": "low"}
    assert body["input"][-1] == {"role": "user", "content": "E 10 km?"}


async def test_openai_incomplete_response_is_a_completion_with_its_reason() -> None:
    adapter, _ = openai_adapter(
        lambda _: stream_response(openai_stream(["Parti"], outcome="incomplete"))
    )
    final = (await collect(adapter, OPENAI_REQUEST))[-1]
    assert isinstance(final, Completed)
    assert final.stop_reason == "max_output_tokens"


async def test_openai_failed_response_carries_its_billed_usage() -> None:
    adapter, _ = openai_adapter(
        lambda _: stream_response(
            openai_stream(["Parti"], outcome="failed", usage=openai_usage(100, 10))
        )
    )
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, OPENAI_REQUEST)
    assert excinfo.value.code is FailureCode.FAILED
    assert excinfo.value.billed_usage == TokenUsage(input_tokens=100, output_tokens=10)


async def test_openai_stream_without_a_final_event_is_incomplete() -> None:
    for outcome, code in (("none", FailureCode.INCOMPLETE), ("error", FailureCode.ERROR)):
        events = openai_stream(["Parti"], outcome=outcome)
        adapter, _ = openai_adapter(lambda _, e=events: stream_response(e))  # type: ignore[misc]
        with pytest.raises(ProviderFailure) as excinfo:
            await collect(adapter, OPENAI_REQUEST)
        assert excinfo.value.code is code


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (openai_error(401, "invalid_request_error", "bad", "invalid_api_key"), FailureCode.AUTH),
        (
            openai_error(404, "invalid_request_error", "no model", "model_not_found"),
            FailureCode.MODEL_NOT_FOUND,
        ),
        (openai_error(429, "requests", "slow", "rate_limit_exceeded"), FailureCode.RATE_LIMITED),
        (
            openai_error(429, "insufficient_quota", "quota", "insufficient_quota"),
            FailureCode.SPEND_LIMIT,
        ),
        (openai_error(400, "invalid_request_error", "bad", None), FailureCode.REJECTED),
        (openai_error(503, "server_error", "busy", None), FailureCode.UNAVAILABLE),
    ],
)  # fmt: skip
async def test_openai_http_errors_are_mapped(response: httpx2.Response, code: FailureCode) -> None:
    adapter, _ = openai_adapter(lambda _: response)
    with pytest.raises(ProviderFailure) as excinfo:
        await collect(adapter, OPENAI_REQUEST)
    assert excinfo.value.code is code


async def test_openai_counts_input_tokens_and_checks_models() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            return httpx2.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 61}
            )
        if request.url.path == "/v1/models/gpt-6-luna":
            return httpx2.Response(
                200,
                json={"id": "gpt-6-luna", "object": "model", "created": 1, "owned_by": "openai"},
            )
        return openai_error(404, "invalid_request_error", "no", "model_not_found")

    adapter, provider = openai_adapter(handler)
    assert await adapter.count_input_tokens(OPENAI_REQUEST) == 61
    assert provider.body()["instructions"] == "Answer briefly."
    check = await adapter.check_model("gpt-6-luna")
    assert (check.available, check.context_limit) == (True, None)
    assert (await adapter.check_model("gpt-nope")).available is False


async def test_counting_failures_surface_as_provider_failures() -> None:
    adapter, _ = anthropic(lambda _: anthropic_error(401, "authentication_error", "bad key"))
    with pytest.raises(ProviderFailure) as excinfo:
        await adapter.count_input_tokens(REQUEST)
    assert excinfo.value.code is FailureCode.AUTH
