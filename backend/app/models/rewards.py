"""Reward rules and the per-day reward ledger (engine arrives in Phase 2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid, utcnow


class RewardRule(Base):
    """A versioned reward parameter, matched by activity date.

    The database rejects overlapping enabled ranges for the same ``activity_type``
    (EXCLUDE constraint), so exactly one rule applies to any given day.
    """

    __tablename__ = "reward_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    activity_type: Mapped[str]
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    daily_limit: Mapped[int | None]
    effective_from: Mapped[date]
    effective_until: Mapped[date | None]
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DailyReward(Base):
    __tablename__ = "daily_rewards"
    __table_args__ = (
        UniqueConstraint("user_id", "activity_date", name="uq_daily_rewards_user_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    activity_date: Mapped[date]
    steps_counted: Mapped[int] = mapped_column(default=0)
    minutes_counted: Mapped[int] = mapped_column(default=0)
    step_credits: Mapped[int] = mapped_column(default=0)
    workout_credits: Mapped[int] = mapped_column(default=0)
    entitled_credits: Mapped[int] = mapped_column(default=0)
    credits_awarded: Mapped[int] = mapped_column(default=0)
    rules_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_test_data: Mapped[bool] = mapped_column(default=False)
    calculated_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
