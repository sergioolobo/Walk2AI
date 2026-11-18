"""The pricing engine, as pure functions.

    cost = Σ (tokens in category × price of category) / 1,000,000

Prices come from a versioned ``model_pricing`` row (``PriceSheet``), never from code.
Everything is ``Decimal``; credits are integers. Nothing here touches the database, the
network or the clock, so the same usage and the same price sheet always give the same
cost — which is what lets every historical charge be recomputed from the usage record.

Rounding rules (docs/DECISIONS.md, D-09):
  * the cost in money is kept exact (it is stored to 8 decimal places, rounded up);
  * credits are rounded **up**, with a minimum of 1 WC for anything billable, so the
    wallet can only ever err on the side of spending less real money than was earned.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.models.ai import ModelPricing

MILLION = Decimal(1_000_000)
STORED_MONEY = Decimal("0.00000001")  # NUMERIC(18,8)

# Keys in ``model_pricing.extra_prices`` for the long-prompt tier of the cache categories
# (for example Claude Haiku 5.5 above 100,000 prompt tokens).
LONG_CACHED_INPUT = "long_context_cached_input_price"
LONG_CACHE_WRITE = "long_context_cache_write_price"
LONG_CACHE_WRITE_1H = "long_context_cache_write_1h_price"


@dataclass(frozen=True)
class TokenUsage:
    """Usage in one vocabulary, whatever the provider.

    The categories do not overlap: ``input_tokens`` is the *uncached* input only.
    ``reasoning_tokens`` is the part of ``output_tokens`` spent thinking; it is already
    included in ``output_tokens`` and is reported for information, not billed twice.
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0  # short-lived cache writes
    cache_write_1h_tokens: int = 0  # long-lived cache writes
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_1h_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens are part of the output and cannot exceed it")

    @property
    def prompt_tokens(self) -> int:
        """Every input token the model read, cached or not: what prompt-size tiers count."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def billable(self) -> bool:
        return self.total_tokens > 0


@dataclass(frozen=True)
class TierPrices:
    """Per-million prices for every category, for one prompt-size tier."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    cache_write_1h: Decimal


@dataclass(frozen=True)
class PriceSheet:
    """A ``model_pricing`` row, ready to compute with."""

    currency: str
    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None
    cache_write_1h: Decimal | None = None
    long_context_threshold: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None
    long_cache_read: Decimal | None = None
    long_cache_write: Decimal | None = None
    long_cache_write_1h: Decimal | None = None

    @classmethod
    def from_row(cls, row: ModelPricing) -> "PriceSheet":
        extra: dict[str, Any] = row.extra_prices or {}

        def optional(key: str) -> Decimal | None:
            value = extra.get(key)
            return Decimal(str(value)) if value is not None else None

        return cls(
            currency=row.currency,
            input=row.input_price,
            output=row.output_price,
            cache_read=row.cached_input_price,
            cache_write=row.cache_write_price,
            cache_write_1h=row.cache_write_1h_price,
            long_context_threshold=row.long_context_threshold,
            long_input=row.long_context_input_price,
            long_output=row.long_context_output_price,
            long_cache_read=optional(LONG_CACHED_INPUT),
            long_cache_write=optional(LONG_CACHE_WRITE),
            long_cache_write_1h=optional(LONG_CACHE_WRITE_1H),
        )

    def is_long(self, prompt_tokens: int) -> bool:
        """Whether a request with this many prompt tokens pays the long-prompt prices.

        Providers apply the higher tier to the *whole* request once the prompt is over
        the threshold, not just to the tokens beyond it.
        """
        return self.long_context_threshold is not None and prompt_tokens > (
            self.long_context_threshold
        )

    def tier(self, prompt_tokens: int) -> TierPrices:
        """Prices that apply to a request of this prompt size.

        A cache category without its own price is billed at that tier's input price:
        cached input is included in the input count on some providers, and charging it at
        the full input price can only over-estimate, never under-estimate.
        """
        if self.is_long(prompt_tokens):
            if self.long_input is None or self.long_output is None:
                raise ValueError("a long-prompt threshold requires long-prompt prices")
            base_input, base_output = self.long_input, self.long_output
            read, write, write_1h = (
                self.long_cache_read,
                self.long_cache_write,
                self.long_cache_write_1h,
            )
        else:
            base_input, base_output = self.input, self.output
            read, write, write_1h = self.cache_read, self.cache_write, self.cache_write_1h
        return TierPrices(
            input=base_input,
            output=base_output,
            cache_read=read if read is not None else base_input,
            cache_write=write if write is not None else base_input,
            cache_write_1h=write_1h if write_1h is not None else base_input,
        )


def cost(usage: TokenUsage, sheet: PriceSheet) -> Decimal:
    """Exact cost of a request in the sheet's currency."""
    prices = sheet.tier(usage.prompt_tokens)
    total = (
        Decimal(usage.input_tokens) * prices.input
        + Decimal(usage.cache_read_tokens) * prices.cache_read
        + Decimal(usage.cache_write_tokens) * prices.cache_write
        + Decimal(usage.cache_write_1h_tokens) * prices.cache_write_1h
        + Decimal(usage.output_tokens) * prices.output
    )
    return total / MILLION if total else Decimal(0)  # Decimal(...) on this line: exact


def worst_case_cost(prompt_tokens: int, max_output_tokens: int, sheet: PriceSheet) -> Decimal:
    """The most a request can cost: every prompt token at the dearest input category of
    its tier, and every allowed output token used.

    ``max_output_tokens`` bounds everything the model generates, reasoning included, on
    both providers, so this is a true upper bound for the given prompt size.
    """
    if prompt_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    prices = sheet.tier(prompt_tokens)
    dearest_input = max(prices.input, prices.cache_write, prices.cache_write_1h, prices.cache_read)
    total = Decimal(prompt_tokens) * dearest_input + Decimal(max_output_tokens) * prices.output
    return total / MILLION if total else Decimal(0)  # Decimal(...) on this line: exact


@dataclass(frozen=True)
class Conversion:
    """How an amount in a price currency becomes Walk Credits."""

    to_eur: Decimal  # 1 unit of the price currency in EUR (1 for EUR itself)
    wc_per_eur: Decimal  # the internal economy: 10,000 initially (1,000 WC = EUR 0.10)

    def __post_init__(self) -> None:
        if self.to_eur <= 0 or self.wc_per_eur <= 0:
            raise ValueError("conversion rates must be positive")


def to_eur(amount: Decimal, conversion: Conversion) -> Decimal:
    return amount * conversion.to_eur


def credits_for(amount: Decimal, conversion: Conversion) -> int:
    """Walk Credits for an amount in the price currency: rounded up, at least 1 WC for
    anything billable, 0 for nothing."""
    if amount < 0:
        raise ValueError("an amount cannot be negative")
    if amount == 0:
        return 0
    wc = (amount * conversion.to_eur * conversion.wc_per_eur).to_integral_value(
        rounding=ROUND_CEILING
    )
    return max(1, int(wc))


def stored(amount: Decimal) -> Decimal:
    """Money as stored (NUMERIC(18,8)): rounded up, so a recorded cost is never less
    than the exact one."""
    return amount.quantize(STORED_MONEY, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class Charge:
    """The full financial outcome of one request's usage."""

    cost: Decimal  # exact, price currency
    currency: str
    cost_eur: Decimal  # exact
    credits: int


def charge_for(usage: TokenUsage, sheet: PriceSheet, conversion: Conversion) -> Charge:
    amount = cost(usage, sheet)
    return Charge(
        cost=amount,
        currency=sheet.currency,
        cost_eur=to_eur(amount, conversion),
        credits=credits_for(amount, conversion),
    )
