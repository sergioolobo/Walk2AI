"""Enumerations stored as TEXT (with CHECK constraints in the migrations)."""

from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class FitnessSource(StrEnum):
    """Where activity data came from.

    Adding a source (for example Strava) means: a new member here, a one-line
    migration extending the CHECK constraints, and an ingestion adapter. Nothing
    else in the pipeline is source-specific.
    """

    APPLE_HEALTHKIT = "apple_healthkit"
    TEST = "test"
    MANUAL = "manual"


class WorkoutCategory(StrEnum):
    STEP_BASED = "step_based"  # already paid through steps: earns no workout minutes
    NON_STEP = "non_step"  # earns workout minutes
    EXCLUDED = "excluded"  # stored, never rewarded


class RewardActivityType(StrEnum):
    STEPS = "steps"
    WORKOUT_MINUTES = "workout_minutes"
    DAILY_TOTAL = "daily_total"


class TransactionType(StrEnum):
    ACTIVITY_REWARD = "ACTIVITY_REWARD"
    REWARD_REVERSAL = "REWARD_REVERSAL"
    AI_USAGE = "AI_USAGE"
    CREDIT_RESERVATION = "CREDIT_RESERVATION"
    CREDIT_RELEASE = "CREDIT_RELEASE"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"


class AIProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ActorType(StrEnum):
    USER = "user"
    ADMIN = "admin"
    DEVICE = "device"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"
