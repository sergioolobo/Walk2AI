"""Conversion rates, model catalogue, pricing, chat and usage (used from Phase 3)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Credits, new_uuid, utcnow

Money = Numeric(18, 8)


class ExchangeRate(Base):
    """A versioned conversion rate: 1 ``base_currency`` = ``rate`` ``quote_currency``.

    Holds both real FX (USD->EUR) and the internal economy (EUR->WC, initially 10000).
    """

    __tablename__ = "exchange_rates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    base_currency: Mapped[str]
    quote_currency: Mapped[str]
    rate: Mapped[Decimal] = mapped_column(Money)
    effective_from: Mapped[datetime]
    effective_until: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AIModel(Base):
    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider", "model_identifier", name="uq_ai_models_provider_identifier"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    provider: Mapped[str]
    model_identifier: Mapped[str]
    display_name: Mapped[str]
    context_limit: Mapped[int | None]
    max_output_tokens: Mapped[int | None]
    supports_streaming: Mapped[bool] = mapped_column(default=True)
    supports_temperature: Mapped[bool] = mapped_column(default=False)
    supports_reasoning_effort: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=False)
    availability_checked_at: Mapped[datetime | None]
    sort_order: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ModelPricing(Base):
    """Prices per 1,000,000 tokens. Only a verified row in force makes a model usable."""

    __tablename__ = "model_pricing"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_models.id", ondelete="RESTRICT"))
    input_price: Mapped[Decimal] = mapped_column(Money)
    output_price: Mapped[Decimal] = mapped_column(Money)
    cached_input_price: Mapped[Decimal | None] = mapped_column(Money)
    cache_write_price: Mapped[Decimal | None] = mapped_column(Money)
    cache_write_1h_price: Mapped[Decimal | None] = mapped_column(Money)
    long_context_threshold: Mapped[int | None]
    long_context_input_price: Mapped[Decimal | None] = mapped_column(Money)
    long_context_output_price: Mapped[Decimal | None] = mapped_column(Money)
    extra_prices: Mapped[dict[str, Any]] = mapped_column(default=dict)
    currency: Mapped[str] = mapped_column(default="USD")
    effective_from: Mapped[datetime]
    effective_until: Mapped[datetime | None]
    verified: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[datetime | None]
    source_url: Mapped[str | None]
    notes: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(default="New conversation")
    provider: Mapped[str]
    model: Mapped[str]
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_models.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AIUsage(Base):
    """The financial record of one provider call. Outlives the conversation."""

    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
        Index("ix_ai_usage_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet_reservations.id", ondelete="RESTRICT")
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_models.id", ondelete="RESTRICT")
    )
    pricing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_pricing.id", ondelete="RESTRICT")
    )
    provider: Mapped[str]
    model: Mapped[str]
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    reasoning_tokens: Mapped[int] = mapped_column(default=0)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Money)
    provider_cost: Mapped[Decimal | None] = mapped_column(Money)
    provider_currency: Mapped[str] = mapped_column(default="USD")
    exchange_rate: Mapped[Decimal | None] = mapped_column(Money)
    cost_eur: Mapped[Decimal | None] = mapped_column(Money)
    wc_per_eur: Mapped[Decimal | None] = mapped_column(Money)
    credits_reserved: Mapped[int] = mapped_column(Credits, default=0)
    credits_charged: Mapped[int] = mapped_column(Credits, default=0)
    cost_basis: Mapped[str] = mapped_column(default="none")
    status: Mapped[str] = mapped_column(default="pending")
    streamed: Mapped[bool] = mapped_column(default=False)
    provider_request_id: Mapped[str | None]
    stop_reason: Mapped[str | None]
    error_code: Mapped[str | None]
    error_message: Mapped[str | None]
    duration_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE")
    )
    role: Mapped[str]
    content: Mapped[str]
    status: Mapped[str] = mapped_column(default="complete")
    usage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_usage.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
