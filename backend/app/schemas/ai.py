"""AI gateway, conversation and catalogue schemas.

Credits are integers (``*_wc``). Money, prices and rates are decimal strings — never
floats. Prices are per million tokens, in the stated currency.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.schemas.common import RequestModel, ResponseModel
from app.schemas.wallet import DecimalString

MAX_MESSAGE_CHARS = 32_000

Message = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
Price = Annotated[Decimal, Field(ge=0, le=Decimal("100000"), max_digits=18, decimal_places=8)]
Effort = Literal["low", "medium", "high"]


# ------------------------------------------------------------------- the gateway


class ProviderResponse(ResponseModel):
    provider: str
    configured: bool = Field(description="An API key for this provider is set on the server.")


class PricesResponse(ResponseModel):
    currency: str
    input: DecimalString
    output: DecimalString
    cached_input: DecimalString | None
    cache_write: DecimalString | None
    cache_write_1h: DecimalString | None
    long_context_threshold: int | None
    long_context_input: DecimalString | None
    long_context_output: DecimalString | None
    verified: bool
    verified_at: datetime | None
    source_url: str | None


class ModelResponse(ResponseModel):
    id: uuid.UUID
    provider: str
    model: str
    display_name: str
    context_limit: int | None
    max_output_tokens: int = Field(description="The most a request may ask for.")
    default_max_output_tokens: int
    supports_temperature: bool
    supports_reasoning_effort: bool
    prices: PricesResponse = Field(description="Per million tokens.")


class EstimateRequest(RequestModel):
    session_id: uuid.UUID | None = None
    model_id: uuid.UUID
    message: Message
    max_output_tokens: Annotated[int, Field(ge=1, le=128_000)] | None = None
    reasoning_effort: Effort | None = None


class WorstCase(ResponseModel):
    cost: DecimalString
    currency: str
    cost_eur: DecimalString
    credits_wc: int


class EstimateResponse(ResponseModel):
    input_tokens: int
    input_tokens_basis: Literal["provider_count", "local_upper_bound"]
    max_output_tokens: int
    worst_case: WorstCase = Field(
        description="The most this message can cost. This is what is reserved; the charge "
        "afterwards is the actual cost, usually much less."
    )
    available_wc: int
    can_afford: bool
    limit_refusal: str | None = Field(description="The spending limit that would refuse it.")
    limit_detail: str | None


class ChatRequest(RequestModel):
    request_id: uuid.UUID = Field(
        description="Chosen by the client. Sending the same request again is refused (409)."
    )
    session_id: uuid.UUID | None = Field(default=None, description="Null starts a conversation.")
    model_id: uuid.UUID
    message: Message
    max_output_tokens: Annotated[int, Field(ge=1, le=128_000)] | None = None
    temperature: Annotated[Decimal, Field(ge=0, le=2, max_digits=4, decimal_places=2)] | None = None
    reasoning_effort: Effort | None = None
    stream: bool = True


class ChatResult(ResponseModel):
    """The non-streaming answer: the same information the stream's events carry."""

    start: dict[str, Any]
    text: str
    outcome: Literal["usage", "error"]
    result: dict[str, Any]


# ----------------------------------------------------------------- conversations


class SessionCreate(RequestModel):
    model_id: uuid.UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None


class SessionUpdate(RequestModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    model_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _something(self) -> "SessionUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class SessionResponse(ResponseModel):
    id: uuid.UUID
    title: str
    provider: str
    model: str
    model_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    messages: int
    credits_charged_wc: int


class MessageUsage(ResponseModel):
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    provider_cost: DecimalString | None
    currency: str
    cost_eur: DecimalString | None
    credits_charged_wc: int
    cost_basis: str
    status: str
    stop_reason: str | None


class MessageResponse(ResponseModel):
    id: uuid.UUID
    role: str
    content: str
    status: str
    created_at: datetime
    usage: MessageUsage | None


class SessionDetail(SessionResponse):
    items: list[MessageResponse]


# ----------------------------------------------------------- catalogue (admin)


class AdminModelResponse(ResponseModel):
    id: uuid.UUID
    provider: str
    model_identifier: str
    display_name: str
    context_limit: int | None
    max_output_tokens: int | None
    supports_streaming: bool
    supports_temperature: bool
    supports_reasoning_effort: bool
    enabled: bool
    availability_checked_at: datetime | None
    sort_order: int
    notes: str | None
    usable: bool
    unavailable_reasons: list[str]
    price_id: uuid.UUID | None


class ModelCreate(RequestModel):
    provider: Literal["anthropic", "openai"]
    model_identifier: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,100}$")]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    context_limit: Annotated[int, Field(ge=1)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1)] | None = None
    supports_temperature: bool = False
    supports_reasoning_effort: bool = False
    sort_order: int = 0
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None


class ModelUpdate(RequestModel):
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    context_limit: Annotated[int, Field(ge=1)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1)] | None = None
    supports_temperature: bool | None = None
    supports_reasoning_effort: bool | None = None
    enabled: bool | None = None
    sort_order: int | None = None
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @model_validator(mode="after")
    def _something(self) -> "ModelUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class ModelCheckResponse(ResponseModel):
    available: bool
    detail: str | None
    model: AdminModelResponse


class PriceFields(RequestModel):
    """Per million tokens."""

    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"
    input_price: Price
    output_price: Price
    cached_input_price: Price | None = None
    cache_write_price: Price | None = None
    cache_write_1h_price: Price | None = None
    long_context_threshold: Annotated[int, Field(ge=1)] | None = None
    long_context_input_price: Price | None = None
    long_context_output_price: Price | None = None
    long_context_cached_input_price: Price | None = None
    long_context_cache_write_price: Price | None = None
    long_context_cache_write_1h_price: Price | None = None


class PriceCreate(PriceFields):
    model_id: uuid.UUID
    effective_from: AwareDatetime | None = Field(default=None, description="Default: now.")
    verified: bool = False
    source_url: Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)] | None = (
        None
    )
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None


class PriceUpdate(RequestModel):
    """Correct an unverified (or not yet effective) price, or mark it verified."""

    prices: PriceFields | None = None
    verified: Literal[True] | None = None
    source_url: Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)] | None = (
        None
    )
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @model_validator(mode="after")
    def _something(self) -> "PriceUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class PriceResponse(ResponseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    currency: str
    input_price: DecimalString
    output_price: DecimalString
    cached_input_price: DecimalString | None
    cache_write_price: DecimalString | None
    cache_write_1h_price: DecimalString | None
    long_context_threshold: int | None
    long_context_input_price: DecimalString | None
    long_context_output_price: DecimalString | None
    extra_prices: dict[str, Any]
    effective_from: datetime
    effective_until: datetime | None
    verified: bool
    verified_at: datetime | None
    source_url: str | None
    notes: str | None


class RateCreate(RequestModel):
    base_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,3}$")]
    quote_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,3}$")]
    rate: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8)] = Field(
        description="1 base_currency = rate quote_currency (e.g. 1 USD = 0.92 EUR)."
    )
    effective_from: AwareDatetime | None = Field(default=None, description="Default: now.")
    source: Annotated[str, StringConstraints(min_length=1, max_length=200)] = "manual"


class RateInForce(ResponseModel):
    rate: DecimalString
    source: str
    effective_from: datetime
    age_hours: int = Field(description="How long this rate has been in force.")


class RateRefreshResponse(ResponseModel):
    outcome: Literal["updated", "unchanged", "kept_manual", "rejected", "unavailable"]
    detail: str
    at: datetime


class RateStatusResponse(ResponseModel):
    mode: Literal["ecb", "manual"] = Field(
        description="ecb: USD->EUR follows the ECB's daily reference rate automatically."
    )
    refresh_minutes: int
    usd_eur: RateInForce | None = Field(description="Null: no USD->EUR rate exists yet.")
    last_refresh: RateRefreshResponse | None = Field(
        description="The last automatic or requested refresh since the server started."
    )


class RateResponse(ResponseModel):
    id: uuid.UUID
    base_currency: str
    quote_currency: str
    rate: DecimalString
    effective_from: datetime
    effective_until: datetime | None
    source: str
    created_at: datetime
