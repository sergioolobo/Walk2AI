"""Initial business configuration, applied by ``python -m app.cli seed``.

These are *data*, not constants in the engine: every value here becomes a versioned
database row that can be superseded through the admin API without touching code.
Seeding is idempotent and never overwrites a value that already exists.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import ExchangeRate
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
    return SeedResult(rules_created, types_created, rates_created)
