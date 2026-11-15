"""Initial business configuration, applied by ``python -m app.cli seed``.

These are *data*, not constants in the engine: every value here becomes a versioned
database row that can be superseded through the admin API without touching code.
Seeding is idempotent and never overwrites a value that already exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIModel, ExchangeRate, ModelPricing
from app.models.enums import RewardActivityType, WorkoutCategory
from app.models.fitness import WorkoutTypeRule
from app.models.rewards import RewardRule

# Rules apply from this date so that activity inside the reward window is covered from
# the first sync onward.
INITIAL_EFFECTIVE_DATE = date(2026, 1, 1)
INITIAL_EFFECTIVE_INSTANT = datetime(2026, 1, 1, tzinfo=UTC)

# (activity type, WC per unit, cap in WC)
INITIAL_REWARD_RULES: tuple[tuple[RewardActivityType, Decimal | None, int | None], ...] = (
    (RewardActivityType.STEPS, Decimal("0.1"), None),  # 1,000 steps = 100 WC
    (RewardActivityType.WORKOUT_MINUTES, Decimal("5"), None),  # 30 minutes = 150 WC
    (RewardActivityType.DAILY_TOTAL, None, 1500),  # daily maximum
)

# 1,000 WC = EUR 0.10
INITIAL_WC_PER_EUR = Decimal("10000")

# HealthKit workout activity types, normalised to snake_case.
#   step_based: already paid through the step count -> no additional workout minutes.
#   non_step:   earns workout minutes.
# Any type not listed is treated as excluded until an administrator classifies it.
INITIAL_WORKOUT_TYPES: dict[str, WorkoutCategory] = {
    "walking": WorkoutCategory.STEP_BASED,
    "running": WorkoutCategory.STEP_BASED,
    "hiking": WorkoutCategory.STEP_BASED,
    "stairs": WorkoutCategory.STEP_BASED,
    "stair_climbing": WorkoutCategory.STEP_BASED,
    "elliptical": WorkoutCategory.STEP_BASED,
    "traditional_strength_training": WorkoutCategory.NON_STEP,
    "functional_strength_training": WorkoutCategory.NON_STEP,
    "core_training": WorkoutCategory.NON_STEP,
    "cycling": WorkoutCategory.NON_STEP,
    "hand_cycling": WorkoutCategory.NON_STEP,
    "swimming": WorkoutCategory.NON_STEP,
    "rowing": WorkoutCategory.NON_STEP,
    "yoga": WorkoutCategory.NON_STEP,
    "pilates": WorkoutCategory.NON_STEP,
    "flexibility": WorkoutCategory.NON_STEP,
    "high_intensity_interval_training": WorkoutCategory.NON_STEP,
    "cross_training": WorkoutCategory.NON_STEP,
    "climbing": WorkoutCategory.NON_STEP,
    "other": WorkoutCategory.EXCLUDED,
}


@dataclass(frozen=True)
class SeedResult:
    reward_rules_created: int
    workout_types_created: int
    exchange_rates_created: int
    models_created: int = 0
    prices_created: int = 0
    prices_verified: int = 0  # seeded unverified prices since confirmed and upgraded


async def seed_initial_configuration(session: AsyncSession) -> SeedResult:
    """Insert whatever initial configuration is missing. The caller commits."""
    rules_created = 0
    for activity_type, rate, limit in INITIAL_REWARD_RULES:
        exists = await session.scalar(
            select(RewardRule.id).where(RewardRule.activity_type == activity_type.value).limit(1)
        )
        if exists is None:
            session.add(
                RewardRule(
                    activity_type=activity_type.value,
                    conversion_rate=rate,
                    daily_limit=limit,
                    effective_from=INITIAL_EFFECTIVE_DATE,
                )
            )
            rules_created += 1

    known_types = set((await session.scalars(select(WorkoutTypeRule.workout_type))).all())
    types_created = 0
    for workout_type, category in INITIAL_WORKOUT_TYPES.items():
        if workout_type not in known_types:
            session.add(WorkoutTypeRule(workout_type=workout_type, category=category.value))
            types_created += 1

    rates_created = 0
    has_rate = await session.scalar(
        select(ExchangeRate.id)
        .where(ExchangeRate.base_currency == "EUR", ExchangeRate.quote_currency == "WC")
        .limit(1)
    )
    if has_rate is None:
        session.add(
            ExchangeRate(
                base_currency="EUR",
                quote_currency="WC",
                rate=INITIAL_WC_PER_EUR,
                effective_from=INITIAL_EFFECTIVE_INSTANT,
                source="initial configuration",
            )
        )
        rates_created += 1

    await session.flush()
    models_created, prices_created, prices_verified = await _seed_catalogue(session)
    return SeedResult(
        rules_created, types_created, rates_created, models_created, prices_created, prices_verified
    )


# ------------------------------------------------------------------- AI catalogue
#
# Every model starts DISABLED. To use one, an administrator confirms it is available to
# this instance's API key (POST /admin/models/{id}/check) and then enables it — which is
# only possible once it has a verified price in force.
#
# Claude prices were read from Anthropic's official pricing page on 2026-11-15:
#   https://platform.claude.com/docs/en/about-claude/pricing
# OpenAI prices were read from OpenAI's official pricing page on 2026-11-15 (standard
# tier): https://developers.openai.com/api/docs/pricing
# OpenAI's usage report does not say which cache lifetime a write used. Walk2AI never asks
# for extended retention, so writes are the default 30-minute kind ("cache_write_price");
# the 12-hour price is recorded as the long-lived one.

ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
PRICES_VERIFIED_AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
# Notes that marked a seeded price as not yet checked. A row still carrying one, and still
# unverified, never charged anything (its model was unusable) and may be corrected.
_UNVERIFIED_SEED_MARKER = "UNVERIFIED:"

D = Decimal


@dataclass(frozen=True)
class SeedModel:
    provider: str
    identifier: str
    name: str
    sort_order: int
    context_limit: int | None
    max_output_tokens: int | None
    supports_reasoning_effort: bool
    price: dict[str, object]
    verified: bool
    notes: str
    source_url: str = ANTHROPIC_PRICING_URL


INITIAL_MODELS: tuple[SeedModel, ...] = (
    SeedModel(
        provider="anthropic",
        identifier="claude-haiku-5-5",
        name="Claude Haiku 5.5",
        sort_order=10,
        context_limit=1_000_000,
        max_output_tokens=128_000,
        supports_reasoning_effort=True,
        price={
            "input_price": D("0.10"),
            "cache_write_price": D("0.125"),
            "cache_write_1h_price": D("0.20"),
            "cached_input_price": D("0.01"),
            "output_price": D("0.50"),
            # Prompts over 100,000 tokens pay higher prices on every category.
            "long_context_threshold": 100_000,
            "long_context_input_price": D("0.50"),
            "long_context_output_price": D("2.50"),
            "extra_prices": {
                "long_context_cache_write_price": "0.625",
                "long_context_cache_write_1h_price": "1",
                "long_context_cached_input_price": "0.05",
            },
        },
        verified=True,
        notes="The least expensive current Claude model.",
    ),
    SeedModel(
        provider="anthropic",
        identifier="claude-sonnet-5-5",
        name="Claude Sonnet 5.5",
        sort_order=20,
        context_limit=1_000_000,
        max_output_tokens=128_000,
        supports_reasoning_effort=True,
        price={
            "input_price": D("2"),
            "cache_write_price": D("2.50"),
            "cache_write_1h_price": D("4"),
            "cached_input_price": D("0.10"),
            "output_price": D("10"),
        },
        verified=True,
        notes="",
    ),
    SeedModel(
        provider="anthropic",
        identifier="claude-opus-5-5",
        name="Claude Opus 5.5",
        sort_order=30,
        context_limit=1_000_000,
        max_output_tokens=128_000,
        supports_reasoning_effort=True,
        price={
            "input_price": D("4"),
            "cache_write_price": D("5"),
            "cache_write_1h_price": D("8"),
            "cached_input_price": D("0.20"),
            "output_price": D("20"),
        },
        verified=True,
        notes="",
    ),
    SeedModel(
        provider="openai",
        identifier="gpt-6-luna",
        name="GPT-6 Luna",
        sort_order=10,
        context_limit=None,
        max_output_tokens=None,
        supports_reasoning_effort=False,
        price={
            "input_price": D("0.10"),
            "cached_input_price": D("0.01"),
            "cache_write_price": D("0.125"),  # 30-minute cache write
            "cache_write_1h_price": D("0.25"),  # 12-hour cache write
            "output_price": D("0.50"),
        },
        verified=True,
        notes="The least expensive current OpenAI model.",
        source_url=OPENAI_PRICING_URL,
    ),
)


def _verify_seeded_price(model: AIModel, prices: Sequence[ModelPricing], seed: SeedModel) -> int:
    """Replace a price this seed once left unverified with the verified figures.

    Only the original seeded row qualifies: unverified (so its model was never usable
    and it never charged anything) and still carrying the seed's marker (so an
    administrator has not edited it). Anything else is left alone.
    """
    if len(prices) != 1:
        return 0
    row = prices[0]
    if row.verified or not (row.notes or "").startswith(_UNVERIFIED_SEED_MARKER):
        return 0
    for name, value in seed.price.items():
        setattr(row, name, value)
    row.verified = True
    row.verified_at = PRICES_VERIFIED_AT
    row.source_url = seed.source_url
    row.notes = seed.notes or None
    if (model.notes or "").startswith(_UNVERIFIED_SEED_MARKER):
        model.notes = seed.notes or None
    return 1


async def _seed_catalogue(session: AsyncSession) -> tuple[int, int, int]:
    models_created = 0
    prices_created = 0
    prices_verified = 0
    for seed in INITIAL_MODELS:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.provider == seed.provider, AIModel.model_identifier == seed.identifier
            )
        )
        if model is None:
            model = AIModel(
                provider=seed.provider,
                model_identifier=seed.identifier,
                display_name=seed.name,
                context_limit=seed.context_limit,
                max_output_tokens=seed.max_output_tokens,
                supports_streaming=True,
                # Current Claude API versions reject sampling parameters; for the OpenAI
                # model it is unknown, so it stays off until an administrator enables it.
                supports_temperature=False,
                supports_reasoning_effort=seed.supports_reasoning_effort,
                enabled=False,
                sort_order=seed.sort_order,
                notes=seed.notes or None,
            )
            session.add(model)
            await session.flush()
            models_created += 1

        prices = (
            await session.scalars(select(ModelPricing).where(ModelPricing.model_id == model.id))
        ).all()
        if not prices:
            session.add(
                ModelPricing(
                    model_id=model.id,
                    currency="USD",
                    effective_from=INITIAL_EFFECTIVE_INSTANT,
                    verified=seed.verified,
                    verified_at=PRICES_VERIFIED_AT if seed.verified else None,
                    source_url=seed.source_url if seed.verified else None,
                    notes=seed.notes or None,
                    **seed.price,
                )
            )
            prices_created += 1
        elif seed.verified:
            prices_verified += _verify_seeded_price(model, prices, seed)
    await session.flush()
    return models_created, prices_created, prices_verified
