"""The one interface the gateway uses for every AI provider.

The gateway never imports a provider SDK. It sends a ``ProviderRequest`` and receives a
stream of ``ProviderEvent``s in one vocabulary; differences between OpenAI and Anthropic
(request shape, stream events, how usage is reported, error classes) are absorbed here.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.services.ai.pricing import TokenUsage


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Turn:
    role: TurnRole
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    turns: tuple[Turn, ...]
    max_output_tokens: int  # bounds everything generated, reasoning included
    system: str | None = None
    temperature: Decimal | None = None  # only sent to models that accept it
    reasoning_effort: str | None = None  # only sent to models that accept it


@dataclass(frozen=True)
class Started:
    """The provider accepted the request and began generating."""

    provider_request_id: str | None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    """The request finished and the provider reported what it used."""

    usage: TokenUsage
    stop_reason: str | None
    provider_request_id: str | None
    served_model: str | None


ProviderEvent = Started | TextDelta | Completed


class FailureCode(StrEnum):
    AUTH = "provider_auth_failed"  # the API key is wrong or lacks permission
    MODEL_NOT_FOUND = "provider_model_not_found"  # not available to this account
    RATE_LIMITED = "provider_rate_limited"
    SPEND_LIMIT = "provider_spend_limit"  # the provider-side budget or prepaid balance ran out
    UNAVAILABLE = "provider_unavailable"  # overloaded, 5xx, or unreachable
    REJECTED = "provider_rejected_request"  # 400-class: the request itself was refused
    TIMEOUT = "provider_timeout"
    INCOMPLETE = "provider_incomplete_stream"  # the stream ended without a final result
    FAILED = "provider_failed"  # the provider reported the response as failed
    ERROR = "provider_error"  # anything else


_USER_MESSAGES = {
    FailureCode.AUTH: "The AI provider rejected the server's API key. An administrator needs "
    "to check the key.",
    FailureCode.MODEL_NOT_FOUND: "This model is not available to the configured API account.",
    FailureCode.RATE_LIMITED: "The AI provider is rate-limiting requests. Try again shortly.",
    FailureCode.SPEND_LIMIT: "The spending limit or prepaid balance at the AI provider has been "
    "reached. Nothing was charged here; an administrator needs to review the provider account.",
    FailureCode.UNAVAILABLE: "The AI provider is unavailable or overloaded. Try again shortly.",
    FailureCode.REJECTED: "The AI provider refused the request.",
    FailureCode.TIMEOUT: "The AI provider did not answer in time.",
    FailureCode.INCOMPLETE: "The answer was cut off before it finished.",
    FailureCode.FAILED: "The AI provider could not complete the answer.",
    FailureCode.ERROR: "The AI provider returned an unexpected error.",
}


class ProviderFailure(Exception):
    """A provider request did not complete.

    ``billed_usage`` is set only when the provider *reported* usage for the failed
    request — then it was billed, and the gateway charges it. Otherwise nothing is
    known to have been billed.
    """

    def __init__(
        self,
        code: FailureCode,
        detail: str | None = None,
        *,
        billed_usage: TokenUsage | None = None,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.detail = detail or _USER_MESSAGES[code]
        self.billed_usage = billed_usage
        self.status_code = status_code
        super().__init__(f"{code.value}: {self.detail}")

    @property
    def user_message(self) -> str:
        return _USER_MESSAGES[self.code]


@dataclass(frozen=True)
class ModelCheck:
    """What the provider says about a model for this account."""

    available: bool
    context_limit: int | None = None
    max_output_tokens: int | None = None
    detail: str | None = None


class ProviderAdapter(Protocol):
    provider: str

    async def count_input_tokens(self, request: ProviderRequest) -> int:
        """Exact prompt size for this request, from the provider's counting endpoint.

        Raises ProviderFailure if the count cannot be obtained.
        """
        ...

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        """Send the request. Yields Started, TextDelta..., then exactly one Completed.

        Raises ProviderFailure on any error.
        """
        ...

    async def check_model(self, model: str) -> ModelCheck:
        """Ask the provider whether the configured account can use this model."""
        ...

    async def aclose(self) -> None: ...
