"""Fitness ingestion tables (populated from Phase 2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid, utcnow

Metres = Numeric(12, 2)
Kilocalories = Numeric(10, 2)


class FitnessSyncBatch(Base):
    __tablename__ = "fitness_sync_batches"
    __table_args__ = (
        UniqueConstraint("user_id", "client_batch_id", name="uq_fitness_sync_batches_user_batch"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    device_token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device_tokens.id", ondelete="SET NULL")
    )
    client_batch_id: Mapped[uuid.UUID]
    source: Mapped[str]
    is_test_data: Mapped[bool] = mapped_column(default=False)
    payload_sha256: Mapped[str]
    days_received: Mapped[int] = mapped_column(default=0)
    workouts_received: Mapped[int] = mapped_column(default=0)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class FitnessDaily(Base):
    __tablename__ = "fitness_daily"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "activity_date", "source", name="uq_fitness_daily_user_date_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    activity_date: Mapped[date]
    source: Mapped[str]
    steps: Mapped[int] = mapped_column(default=0)
    walking_distance_m: Mapped[Decimal | None] = mapped_column(Metres)
    running_distance_m: Mapped[Decimal | None] = mapped_column(Metres)
    cycling_distance_m: Mapped[Decimal | None] = mapped_column(Metres)
    active_minutes: Mapped[int] = mapped_column(default=0)
    active_energy_kcal: Mapped[Decimal | None] = mapped_column(Kilocalories)
    device_timezone: Mapped[str | None]
    is_test_data: Mapped[bool] = mapped_column(default=False)
    last_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fitness_sync_batches.id", ondelete="SET NULL")
    )
    synced_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WorkoutEvent(Base):
    __tablename__ = "workout_events"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "external_id", name="uq_workout_events_user_source_external"
        ),
        Index("ix_workout_events_user_start", "user_id", "start_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source: Mapped[str]
    external_id: Mapped[str]
    workout_type: Mapped[str]
    start_time: Mapped[datetime]
    end_time: Mapped[datetime]
    duration_minutes: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    distance_m: Mapped[Decimal | None] = mapped_column(Metres)
    active_energy_kcal: Mapped[Decimal | None] = mapped_column(Kilocalories)
    origin_bundle_id: Mapped[str | None]
    origin_device: Mapped[str | None]
    was_user_entered: Mapped[bool] = mapped_column(default=False)
    verified: Mapped[bool] = mapped_column(default=False)
    is_test_data: Mapped[bool] = mapped_column(default=False)
    excluded_from_rewards: Mapped[bool] = mapped_column(default=False)
    exclusion_reason: Mapped[str | None]
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workout_events.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None]
    last_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fitness_sync_batches.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WorkoutTypeRule(Base):
    __tablename__ = "workout_type_rules"

    workout_type: Mapped[str] = mapped_column(primary_key=True)
    category: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
