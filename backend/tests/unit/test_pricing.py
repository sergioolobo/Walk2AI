"""The pricing engine: exact costs from versioned prices, and credits that never
under-charge. Prices below are the official Claude API prices verified on 2026-11-15
(https://platform.claude.com/docs/en/about-claude/pricing), in USD per million tokens."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models.ai import ModelPricing
from app.services.ai.pricing import (
    LONG_CACHE_WRITE,
    LONG_CACHE_WRITE_1H,
    LONG_CACHED_INPUT,
    Conversion,
    PriceSheet,
    TokenUsage,
    charge_for,
    cost,
    credits_for,
    stored,
    worst_case_cost,
)

D = Decimal

HAIKU_5_5 = PriceSheet(
    currency="USD",
    input=D("0.10"),
    output=D("0.50"),
    cache_read=D("0.01"),
    cache_write=D("0.125"),
    cache_write_1h=D("0.20"),
    long_context_threshold=100_000,
    long_input=D("0.50"),
    long_output=D("2.50"),
    long_cache_read=D("0.05"),
    long_cache_write=D("0.625"),
    long_cache_write_1h=D("1"),
)
SONNET_5_5 = PriceSheet(
    currency="USD",
    input=D("2"),
    output=D("10"),
    cache_read=D("0.10"),
    cache_write=D("2.50"),
    cache_write_1h=D("4"),
)
OPUS_5_5 = PriceSheet(
    currency="USD",
    input=D("4"),
    output=D("20"),
    cache_read=D("0.20"),
    cache_write=D("5"),
    cache_write_1h=D("8"),
)
# 1 USD = 0.92 EUR (an assumed rate for the examples); 1,000 WC = EUR 0.10.
USD_AT_092 = Conversion(to_eur=D("0.92"), wc_per_eur=D("10000"))


# --------------------------------------------------------------- the basic formula


def test_specification_formula_input_plus_output() -> None:
    """Actual cost = input x input price / 1M + output x output price / 1M."""
    usage = TokenUsage(input_tokens=2000, output_tokens=1000)
    assert cost(usage, HAIKU_5_5) == D("0.0007")
    assert cost(usage, SONNET_5_5) == D("0.014")
    assert cost(usage, OPUS_5_5) == D("0.028")


@pytest.mark.parametrize(
    ("sheet", "credits"),
    [(HAIKU_5_5, 7), (SONNET_5_5, 129), (OPUS_5_5, 258)],
)
def test_architecture_examples_in_walk_credits(sheet: PriceSheet, credits: int) -> None:
    """The figures in docs/ARCHITECTURE.md: a 2,000-in / 1,000-out request."""
    charge = charge_for(TokenUsage(input_tokens=2000, output_tokens=1000), sheet, USD_AT_092)
    assert charge.credits == credits
    assert charge.currency == "USD"
    assert charge.cost_eur == charge.cost * D("0.92")


def test_every_category_is_priced_separately() -> None:
    usage = TokenUsage(
        input_tokens=1_000,
        cache_read_tokens=20_000,
        cache_write_tokens=3_000,
        cache_write_1h_tokens=4_000,
        output_tokens=2_500,
        reasoning_tokens=1_000,
    )
    expected = (
        D(1_000) * D("2")
        + D(20_000) * D("0.10")
        + D(3_000) * D("2.50")
        + D(4_000) * D("4")
        + D(2_500) * D("10")
    ) / D(1_000_000)
    assert cost(usage, SONNET_5_5) == expected == D("0.0525")


def test_reasoning_tokens_are_part_of_the_output_not_billed_twice() -> None:
    plain = TokenUsage(input_tokens=100, output_tokens=900)
    thoughtful = TokenUsage(input_tokens=100, output_tokens=900, reasoning_tokens=800)
    assert cost(plain, OPUS_5_5) == cost(thoughtful, OPUS_5_5)


def test_nothing_costs_nothing() -> None:
    assert cost(TokenUsage(), OPUS_5_5) == 0
    assert charge_for(TokenUsage(), OPUS_5_5, USD_AT_092).credits == 0
    assert not TokenUsage().billable


# ------------------------------------------------------------- long-prompt pricing


def test_haiku_long_prompt_tier_applies_to_the_whole_request() -> None:
    at_threshold = TokenUsage(input_tokens=100_000, output_tokens=1_000)
    over = TokenUsage(input_tokens=100_001, output_tokens=1_000)

    assert cost(at_threshold, HAIKU_5_5) == (D(100_000) * D("0.10") + D(1_000) * D("0.50")) / D(
        1_000_000
    )
    # One token over: every token, output included, moves to the higher prices.
    assert cost(over, HAIKU_5_5) == (D(100_001) * D("0.50") + D(1_000) * D("2.50")) / D(1_000_000)


def test_cached_tokens_count_towards_the_prompt_size() -> None:
    """A request whose prompt is mostly cache reads is still a long prompt."""
    usage = TokenUsage(input_tokens=5_000, cache_read_tokens=96_000, output_tokens=500)
    assert HAIKU_5_5.is_long(usage.prompt_tokens)
    assert cost(usage, HAIKU_5_5) == (
        D(5_000) * D("0.50") + D(96_000) * D("0.05") + D(500) * D("2.50")
    ) / D(1_000_000)


def test_long_prompt_cache_prices_come_from_extra_prices() -> None:
    usage = TokenUsage(input_tokens=60_000, cache_write_tokens=30_000, cache_write_1h_tokens=20_000)
    assert cost(usage, HAIKU_5_5) == (
        D(60_000) * D("0.50") + D(30_000) * D("0.625") + D(20_000) * D("1")
    ) / D(1_000_000)


def test_models_without_a_tier_pay_the_same_rate_at_any_size() -> None:
    small = cost(TokenUsage(input_tokens=1_000), SONNET_5_5)
    huge = cost(TokenUsage(input_tokens=900_000), SONNET_5_5)
    assert huge == small * 900


def test_a_cache_category_without_its_own_price_is_charged_at_the_input_price() -> None:
    """Never an invented discount: an unknown price is the full input price."""
    sheet = PriceSheet(currency="USD", input=D("1"), output=D("4"))
    usage = TokenUsage(input_tokens=100, cache_read_tokens=200, cache_write_tokens=300)
    assert cost(usage, sheet) == D(600) * D("1") / D(1_000_000)


def test_a_threshold_without_long_prices_is_refused() -> None:
    broken = PriceSheet(currency="USD", input=D("1"), output=D("2"), long_context_threshold=10)
    with pytest.raises(ValueError, match="long-prompt prices"):
        cost(TokenUsage(input_tokens=11), broken)


# ---------------------------------------------------------------- worst case


def test_worst_case_assumes_every_output_token_and_the_dearest_input_price() -> None:
    # Prompt priced as if it were all 1-hour cache writes (the dearest input category).
    assert worst_case_cost(10_000, 2_000, SONNET_5_5) == (
        D(10_000) * D("4") + D(2_000) * D("10")
    ) / D(1_000_000)
    plain = PriceSheet(currency="USD", input=D("1"), output=D("5"))
    assert worst_case_cost(1_000, 1_000, plain) == D("0.006")
    assert worst_case_cost(0, 0, plain) == 0


def test_worst_case_uses_the_long_prompt_tier_when_the_prompt_is_long() -> None:
    assert worst_case_cost(150_000, 1_000, HAIKU_5_5) == (
        D(150_000) * D("1") + D(1_000) * D("2.50")
    ) / D(1_000_000)


usages = st.builds(
    TokenUsage,
    input_tokens=st.integers(0, 300_000),
    cache_read_tokens=st.integers(0, 300_000),
    cache_write_tokens=st.integers(0, 100_000),
    cache_write_1h_tokens=st.integers(0, 100_000),
    output_tokens=st.integers(0, 128_000),
)


@given(usage=usages, spare_output=st.integers(0, 10_000))
def test_the_worst_case_is_never_below_the_actual_cost(
    usage: TokenUsage, spare_output: int
) -> None:
    """For any usage within the same prompt size and output allowance, the reservation
    covers the charge. This is what lets the wallet hold credits before a request and
    never have to collect more afterwards."""
    for sheet in (HAIKU_5_5, SONNET_5_5, OPUS_5_5):
        bound = worst_case_cost(usage.prompt_tokens, usage.output_tokens + spare_output, sheet)
        assert cost(usage, sheet) <= bound
        conversion = Conversion(to_eur=D("0.97"), wc_per_eur=D("10000"))
        assert credits_for(cost(usage, sheet), conversion) <= max(1, credits_for(bound, conversion))


@given(usage=usages)
def test_cost_is_never_negative_and_is_exact(usage: TokenUsage) -> None:
    amount = cost(usage, HAIKU_5_5)
    assert amount >= 0
    assert isinstance(amount, Decimal)
    assert (amount == 0) == (not usage.billable)


# ------------------------------------------------------------------- credits


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (D("0"), 0),
        (D("0.00000001"), 1),  # anything billable costs at least 1 WC
        (D("0.0001"), 1),  # 0.000092 EUR = 0.92 WC -> 1
        (D("0.00011"), 2),  # 1.012 WC -> 2: always rounded up
        (D("0.0007"), 7),  # 6.44 -> 7
        (D("0.10869565"), 1000),  # 0.0999999998 EUR -> 999.999998 -> 1,000
        (D("1"), 9200),
    ],
)
def test_credits_are_rounded_up(amount: Decimal, expected: int) -> None:
    assert credits_for(amount, USD_AT_092) == expected


def test_credits_in_eur_priced_models_need_no_exchange() -> None:
    eur = Conversion(to_eur=D("1"), wc_per_eur=D("10000"))
    assert credits_for(D("0.10"), eur) == 1000  # 1,000 WC = EUR 0.10, by definition


def test_conversions_and_amounts_must_be_sane() -> None:
    with pytest.raises(ValueError):
        Conversion(to_eur=D("0"), wc_per_eur=D("10000"))
    with pytest.raises(ValueError):
        Conversion(to_eur=D("1"), wc_per_eur=D("-1"))
    with pytest.raises(ValueError):
        credits_for(D("-0.01"), USD_AT_092)


def test_stored_money_is_rounded_up_to_eight_places() -> None:
    assert stored(D("0.000000001")) == D("0.00000001")
    assert stored(D("0.0007")) == D("0.00070000")
    assert stored(D("0.123456781")) == D("0.12345679")


@pytest.mark.parametrize(
    "bad",
    [
        {"input_tokens": -1},
        {"output_tokens": -5},
        {"input_tokens": 1.5},
        {"output_tokens": 10, "reasoning_tokens": 11},
    ],
)
def test_usage_must_be_whole_non_negative_numbers(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TokenUsage(**bad)  # type: ignore[arg-type]


def test_a_price_sheet_is_built_from_a_pricing_row() -> None:
    row = ModelPricing(
        model_id=None,
        input_price=D("0.10"),
        output_price=D("0.50"),
        cached_input_price=D("0.01"),
        cache_write_price=D("0.125"),
        cache_write_1h_price=D("0.20"),
        long_context_threshold=100_000,
        long_context_input_price=D("0.50"),
        long_context_output_price=D("2.50"),
        extra_prices={
            LONG_CACHED_INPUT: "0.05",
            LONG_CACHE_WRITE: "0.625",
            LONG_CACHE_WRITE_1H: "1",
        },
        currency="USD",
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert PriceSheet.from_row(row) == HAIKU_5_5
