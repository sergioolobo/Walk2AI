"""Provider HTTP responses for tests, in the shapes the official APIs send.

These are served to the *real* SDK clients through an ``httpx2.MockTransport``, so the
SDKs' own parsing runs exactly as it does in production. No test ever reaches a real
provider or incurs a cost.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2


def sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def stream_response(events: list[tuple[str, dict[str, Any]]]) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))


def error_response(status: int, body: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(status, json=body)


# ------------------------------------------------------------------- Anthropic


def anthropic_stream(
    texts: list[str],
    *,
    model: str = "claude-haiku-5-5",
    input_tokens: int = 25,
    output_tokens: int = 120,
    thinking_tokens: int | None = 80,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    stop_reason: str = "end_turn",
    message_id: str = "msg_01TEST",
    finish: bool = True,
    error: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """A Messages API stream: a (silent) thinking block, then text, then usage."""
    start_usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": 1,
        "cache_creation_input_tokens": cache_write_5m + cache_write_1h,
        "cache_read_input_tokens": cache_read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cache_write_5m,
            "ephemeral_1h_input_tokens": cache_write_1h,
        },
    }
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": start_usage,
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "EqQBCgIYAhIM"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    events += [
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": t}},
        )
        for t in texts
    ]
    if error is not None:
        events.append(("error", {"type": "error", "error": error}))
        return events
    events.append(("content_block_stop", {"type": "content_block_stop", "index": 1}))
    if not finish:
        return events
    final_usage: dict[str, Any] = {"output_tokens": output_tokens}
    if thinking_tokens is not None:
        final_usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": final_usage,
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return events


def anthropic_error(
    status: int, kind: str, message: str, code: str | None = None
) -> httpx2.Response:
    error: dict[str, Any] = {"type": kind, "message": message}
    if code:
        error["details"] = {"error_code": code}
    return error_response(status, {"type": "error", "error": error, "request_id": "req_test"})


def anthropic_model(model: str = "claude-haiku-5-5") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "type": "model",
            "id": model,
            "display_name": "Claude Haiku 5.5",
            "created_at": "2026-10-07T00:00:00Z",
            "lifecycle": "active",
            "max_input_tokens": 1_000_000,
            "max_tokens": 128_000,
        },
    )


# ---------------------------------------------------------------------- OpenAI


def _openai_response(
    *,
    response_id: str,
    model: str,
    status: str,
    usage: dict[str, Any] | None,
    incomplete_reason: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_763_200_000,
        "model": model,
        "status": status,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "error": error,
    }


def openai_usage(
    input_tokens: int,
    output_tokens: int,
    *,
    cached: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def openai_stream(
    texts: list[str],
    *,
    model: str = "gpt-6-luna",
    usage: dict[str, Any] | None = None,
    response_id: str = "resp_TEST",
    outcome: str = "completed",  # completed | incomplete | failed | error | none
    incomplete_reason: str = "max_output_tokens",
) -> list[tuple[str, dict[str, Any]]]:
    usage = usage if usage is not None else openai_usage(120, 50, cached=64, reasoning=20)
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "response.created",
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": _openai_response(
                    response_id=response_id, model=model, status="in_progress", usage=None
                ),
            },
        )
    ]
    for index, text in enumerate(texts, start=1):
        events.append(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": index,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                    "logprobs": [],
                },
            )
        )
    sequence = len(texts) + 1
    if outcome == "completed":
        events.append(
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": sequence,
                    "response": _openai_response(
                        response_id=response_id, model=model, status="completed", usage=usage
                    ),
                },
            )
        )
    elif outcome == "incomplete":
        events.append(
            (
                "response.incomplete",
                {
                    "type": "response.incomplete",
                    "sequence_number": sequence,
                    "response": _openai_response(
                        response_id=response_id,
                        model=model,
                        status="incomplete",
                        usage=usage,
                        incomplete_reason=incomplete_reason,
                    ),
                },
            )
        )
    elif outcome == "failed":
        events.append(
            (
                "response.failed",
                {
                    "type": "response.failed",
                    "sequence_number": sequence,
                    "response": _openai_response(
                        response_id=response_id,
                        model=model,
                        status="failed",
                        usage=usage,
                        error={"code": "server_error", "message": "The model failed."},
                    ),
                },
            )
        )
    elif outcome == "error":
        events.append(
            (
                "error",
                {
                    "type": "error",
                    "sequence_number": sequence,
                    "code": "server_error",
                    "message": "Something broke.",
                    "param": None,
                },
            )
        )
    return events


def openai_error(status: int, kind: str, message: str, code: str | None = None) -> httpx2.Response:
    return error_response(
        status, {"error": {"message": message, "type": kind, "code": code, "param": None}}
    )


# ---------------------------------------------------------------- the transport


@dataclass
class RecordingProvider:
    """Answers SDK requests with scripted responses and remembers what was sent."""

    handler: Callable[[httpx2.Request], httpx2.Response]
    requests: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def body(self, index: int = -1) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded
