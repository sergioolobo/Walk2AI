"""The model catalogue: which models exist, at what price, and whether they may be used.

A model can be used for a request only when *all* of these hold at that moment:

  * an administrator enabled it;
  * its availability was confirmed against the provider with this instance's API key;
  * that provider has an API key configured;
  * it has a **verified** price in force (a price that cannot be traced to the
    provider's own pricing page is stored as unverified, and makes the model unusable);
  * the price's currency can be converted to EUR, and EUR to Walk Credits.

Otherwise it is unavailable, with the reasons listed — a request is never priced with a
guess. Prices and conversion rates are versioned: a row in force is never rewritten, it
is closed and succeeded, so every past charge stays explainable.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.ai import AIModel, ExchangeRate, ModelPricing
from app.services.ai.pricing import (
    LONG_CACHE_WRITE,
    LONG_CACHE_WRITE_1H,
    LONG_CACHED_INPUT,
    Conversion,
    PriceSheet,
)
from app.services.ai.providers import KNOWN_PROVIDERS, ModelCheck

EUR = "EUR"
WALK_CREDITS = "WC"

DISABLED = "disabled"
NOT_CHECKED = "availability_not_checked"
NO_PROVIDER = "provider_not_configured"
NO_PRICE = "no_price"
UNVERIFIED_PRICE = "price_unverified"
NO_EXCHANGE_RATE = "no_exchange_rate"
NO_CREDIT_RATE = "no_credit_rate"

REASON_TEXT = {
    DISABLED: "The model is disabled.",
    NOT_CHECKED: "Its availability has not been confirmed with this instance's API key.",
    NO_PROVIDER: "No API key is configured for its provider.",
    NO_PRICE: "It has no price in force.",
    UNVERIFIED_PRICE: "Its price has not been verified against the provider's pricing page.",
    NO_EXCHANGE_RATE: "No exchange rate to EUR is configured for its price currency.",
    NO_CREDIT_RATE: "No EUR to Walk Credits rate is configured.",
}


class ModelUnavailable(Conflict):
    code = "model_unavailable"
    title = "This model cannot be used right now"


class CatalogueConflict(Conflict):
    code = "catalogue_conflict"
    title = "The change conflicts with the catalogue"


@dataclass(frozen=True)
class Rates:
    """The conversion rates in force at one instant, with the rows they came from."""

    to_eur: Decimal | None
    wc_per_eur: Decimal | None

    def conversion(self) -> Conversion | None:
        if self.to_eur is None or self.wc_per_eur is None:
            return None
        return Conversion(to_eur=self.to_eur, wc_per_eur=self.wc_per_eur)


@dataclass(frozen=True)
class ModelStatus:
    model: AIModel
    price: ModelPricing | None
    rates: Rates
    reasons: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class UsableModel:
    """Everything needed to reserve for, call and charge one request."""

    model: AIModel
    price: ModelPricing
    sheet: PriceSheet
    conversion: Conversion


def _in_force(column_from: Any, column_until: Any, at: datetime) -> tuple[Any, ...]:
    return (column_from <= at, or_(column_until.is_(None), column_until > at))


async def price_in_force(
    session: AsyncSession, model_id: uuid.UUID, at: datetime
) -> ModelPricing | None:
    result: ModelPricing | None = await session.scalar(
        select(ModelPricing).where(
            ModelPricing.model_id == model_id,
            *_in_force(ModelPricing.effective_from, ModelPricing.effective_until, at),
        )
    )
    return result


async def rate_in_force(
    session: AsyncSession, base: str, quote: str, at: datetime
) -> ExchangeRate | None:
    result: ExchangeRate | None = await session.scalar(
        select(ExchangeRate).where(
            ExchangeRate.base_currency == base,
            ExchangeRate.quote_currency == quote,
            *_in_force(ExchangeRate.effective_from, ExchangeRate.effective_until, at),
        )
    )
    return result


async def rates_for(session: AsyncSession, currency: str, at: datetime) -> Rates:
    wc = await rate_in_force(session, EUR, WALK_CREDITS, at)
    if currency == EUR:
        to_eur: Decimal | None = Decimal(1)
    else:
        fx = await rate_in_force(session, currency, EUR, at)
        to_eur = fx.rate if fx is not None else None
    return Rates(to_eur=to_eur, wc_per_eur=wc.rate if wc is not None else None)


async def status_of(
    session: AsyncSession, model: AIModel, configured: Collection[str], at: datetime
) -> ModelStatus:
    price = await price_in_force(session, model.id, at)
    rates = await rates_for(session, price.currency if price else "USD", at)
    reasons: list[str] = []
    if not model.enabled:
        reasons.append(DISABLED)
    if model.availability_checked_at is None:
        reasons.append(NOT_CHECKED)
    if model.provider not in configured:
        reasons.append(NO_PROVIDER)
    if price is None:
        reasons.append(NO_PRICE)
    elif not price.verified:
        reasons.append(UNVERIFIED_PRICE)
    if price is not None and rates.to_eur is None:
        reasons.append(NO_EXCHANGE_RATE)
    if rates.wc_per_eur is None:
        reasons.append(NO_CREDIT_RATE)
    return ModelStatus(model=model, price=price, rates=rates, reasons=tuple(reasons))


async def list_statuses(
    session: AsyncSession, configured: Collection[str], at: datetime
) -> list[ModelStatus]:
    models = (
        await session.scalars(
            select(AIModel).order_by(AIModel.provider, AIModel.sort_order, AIModel.display_name)
        )
    ).all()
    return [await status_of(session, model, configured, at) for model in models]


async def resolve(
    session: AsyncSession, model_id: uuid.UUID, configured: Collection[str], at: datetime
) -> UsableModel:
    """The model, ready to use — or ModelUnavailable saying exactly why not."""
    model = await session.get(AIModel, model_id)
    if model is None:
        raise NotFound("Model not found.")
    status = await status_of(session, model, configured, at)
    conversion = status.rates.conversion()
    if not status.usable or status.price is None or conversion is None:
        raise ModelUnavailable(
            " ".join(REASON_TEXT[r] for r in status.reasons) or "The model cannot be used.",
            extra={"reasons": list(status.reasons)},
        )
    return UsableModel(
        model=model,
        price=status.price,
        sheet=PriceSheet.from_row(status.price),
        conversion=conversion,
    )


# ------------------------------------------------------------------ administration


@dataclass(frozen=True)
class PriceInput:
    input_price: Decimal
    output_price: Decimal
    cached_input_price: Decimal | None = None
    cache_write_price: Decimal | None = None
    cache_write_1h_price: Decimal | None = None
    long_context_threshold: int | None = None
    long_context_input_price: Decimal | None = None
    long_context_output_price: Decimal | None = None
    long_context_cached_input_price: Decimal | None = None
    long_context_cache_write_price: Decimal | None = None
    long_context_cache_write_1h_price: Decimal | None = None
    currency: str = "USD"

    def validate(self) -> None:
        has_threshold = self.long_context_threshold is not None
        long_prices = (
            self.long_context_input_price,
            self.long_context_output_price,
            self.long_context_cached_input_price,
            self.long_context_cache_write_price,
            self.long_context_cache_write_1h_price,
        )
        if has_threshold and (
            self.long_context_input_price is None or self.long_context_output_price is None
        ):
            raise ValidationFailed(
                "A long-prompt threshold needs both long-prompt input and output prices."
            )
        if not has_threshold and any(v is not None for v in long_prices):
            raise ValidationFailed("Long-prompt prices need a long-prompt threshold.")
        if has_threshold:
            pairs = (
                (self.cached_input_price, self.long_context_cached_input_price, "cached input"),
                (self.cache_write_price, self.long_context_cache_write_price, "cache write"),
                (
                    self.cache_write_1h_price,
                    self.long_context_cache_write_1h_price,
                    "1h cache write",
                ),
            )
            for base, long, name in pairs:
                if base is not None and long is None:
                    raise ValidationFailed(
                        f"With a long-prompt tier, the {name} price needs a long-prompt price too."
                    )

    def extra_prices(self) -> dict[str, str]:
        extra = {
            LONG_CACHED_INPUT: self.long_context_cached_input_price,
            LONG_CACHE_WRITE: self.long_context_cache_write_price,
            LONG_CACHE_WRITE_1H: self.long_context_cache_write_1h_price,
        }
        return {k: format(v, "f") for k, v in extra.items() if v is not None}


class CatalogueAdmin:
    """Administrative changes to models, prices and conversion rates. The caller commits."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # ---------------------------------------------------------------- models

    async def get_model(self, model_id: uuid.UUID) -> AIModel:
        model = await self._session.get(AIModel, model_id)
        if model is None:
            raise NotFound("Model not found.")
        return model

    async def create_model(self, **fields: Any) -> AIModel:
        if fields["provider"] not in KNOWN_PROVIDERS:
            raise ValidationFailed(f"Unknown provider {fields['provider']!r}.")
        exists = await self._session.scalar(
            select(AIModel.id).where(
                AIModel.provider == fields["provider"],
                AIModel.model_identifier == fields["model_identifier"],
            )
        )
        if exists is not None:
            raise CatalogueConflict("That model is already in the catalogue.")
        now = self._clock.now()
        model = AIModel(**fields, enabled=False, created_at=now, updated_at=now)
        self._session.add(model)
        await self._session.flush()
        return model

    async def update_model(self, model_id: uuid.UUID, changes: dict[str, Any]) -> AIModel:
        model = await self.get_model(model_id)
        if changes.get("enabled") is True and not model.enabled:
            await self._require_enableable(model)
        for name, value in changes.items():
            setattr(model, name, value)
        model.updated_at = self._clock.now()
        await self._session.flush()
        return model

    async def _require_enableable(self, model: AIModel) -> None:
        if model.availability_checked_at is None:
            raise CatalogueConflict(
                "Confirm the model is available to this account first "
                "(POST /admin/models/{id}/check)."
            )
        price = await price_in_force(self._session, model.id, self._clock.now())
        if price is None or not price.verified:
            raise CatalogueConflict("The model needs a verified price in force before enabling.")

    async def record_check(self, model: AIModel, check: ModelCheck) -> AIModel:
        now = self._clock.now()
        if check.available:
            model.availability_checked_at = now
            if check.context_limit:
                model.context_limit = check.context_limit
            if check.max_output_tokens:
                model.max_output_tokens = check.max_output_tokens
        else:
            model.availability_checked_at = None
            model.enabled = False
        model.updated_at = now
        await self._session.flush()
        return model

    # ---------------------------------------------------------------- prices

    async def list_prices(self, model_id: uuid.UUID | None) -> Sequence[ModelPricing]:
        query = select(ModelPricing)
        if model_id is not None:
            query = query.where(ModelPricing.model_id == model_id)
        result = await self._session.scalars(
            query.order_by(ModelPricing.model_id, ModelPricing.effective_from.desc())
        )
        return result.all()

    async def create_price(
        self,
        model_id: uuid.UUID,
        prices: PriceInput,
        *,
        effective_from: datetime | None,
        verified: bool,
        source_url: str | None,
        notes: str | None,
        created_by: uuid.UUID | None,
    ) -> ModelPricing:
        """Add a price version. The version currently open ends where this one starts."""
        await self.get_model(model_id)
        prices.validate()
        if verified and not source_url:
            raise ValidationFailed("A verified price must cite the provider page it came from.")
        now = self._clock.now()
        start = effective_from or now
        if start < now:
            raise ValidationFailed("A price cannot start in the past.")

        rows = (
            await self._session.scalars(
                select(ModelPricing).where(ModelPricing.model_id == model_id).with_for_update()
            )
        ).all()
        for row in rows:
            if row.effective_from >= start:
                raise CatalogueConflict(
                    "A price version starting at or after that moment already exists. "
                    "Edit it, or choose a later start."
                )
            if row.effective_until is None or row.effective_until > start:
                row.effective_until = start
        await self._session.flush()

        row = ModelPricing(
            model_id=model_id,
            input_price=prices.input_price,
            output_price=prices.output_price,
            cached_input_price=prices.cached_input_price,
            cache_write_price=prices.cache_write_price,
            cache_write_1h_price=prices.cache_write_1h_price,
            long_context_threshold=prices.long_context_threshold,
            long_context_input_price=prices.long_context_input_price,
            long_context_output_price=prices.long_context_output_price,
            extra_prices=prices.extra_prices(),
            currency=prices.currency,
            effective_from=start,
            verified=verified,
            verified_at=now if verified else None,
            source_url=source_url,
            notes=notes,
            created_by=created_by,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_price(
        self,
        price_id: uuid.UUID,
        *,
        prices: PriceInput | None,
        verify: bool,
        source_url: str | None,
        notes: str | None,
        fields: frozenset[str],
    ) -> ModelPricing:
        """Correct or verify a price.

        A verified price that has started may have charged requests, so it can never be
        changed: publish a new version instead. An unverified price never charged
        anything (the model was unusable), so it can be corrected and then verified.
        """
        row = await self._session.scalar(
            select(ModelPricing).where(ModelPricing.id == price_id).with_for_update()
        )
        if row is None:
            raise NotFound("Price not found.")
        now = self._clock.now()
        if row.verified and row.effective_from <= now:
            raise CatalogueConflict(
                "This verified price is in force and may have charged requests. "
                "Publish a new price version instead (POST /admin/model-pricing)."
            )
        if prices is not None:
            prices.validate()
            row.input_price = prices.input_price
            row.output_price = prices.output_price
            row.cached_input_price = prices.cached_input_price
            row.cache_write_price = prices.cache_write_price
            row.cache_write_1h_price = prices.cache_write_1h_price
            row.long_context_threshold = prices.long_context_threshold
            row.long_context_input_price = prices.long_context_input_price
            row.long_context_output_price = prices.long_context_output_price
            row.extra_prices = prices.extra_prices()
            row.currency = prices.currency
        if "source_url" in fields:
            row.source_url = source_url
        if "notes" in fields:
            row.notes = notes
        if verify:
            if not row.source_url:
                raise ValidationFailed("A verified price must cite the provider page it came from.")
            row.verified = True
            row.verified_at = now
        await self._session.flush()
        await self._session.refresh(row)
        return row

    # ---------------------------------------------------------------- rates

    async def list_rates(self) -> Sequence[ExchangeRate]:
        result = await self._session.scalars(
            select(ExchangeRate).order_by(
                ExchangeRate.base_currency,
                ExchangeRate.quote_currency,
                ExchangeRate.effective_from.desc(),
            )
        )
        return result.all()

    async def create_rate(
        self,
        base: str,
        quote: str,
        rate: Decimal,
        *,
        effective_from: datetime | None,
        source: str,
        created_by: uuid.UUID | None,
    ) -> ExchangeRate:
        """Add a conversion-rate version from now (or later); the open one ends there."""
        if base == quote:
            raise ValidationFailed("A rate converts between two different currencies.")
        if rate <= 0:
            raise ValidationFailed("A rate must be positive.")
        now = self._clock.now()
        start = effective_from or now
        if start < now:
            raise ValidationFailed("A rate cannot start in the past.")
        rows = (
            await self._session.scalars(
                select(ExchangeRate)
                .where(ExchangeRate.base_currency == base, ExchangeRate.quote_currency == quote)
                .with_for_update()
            )
        ).all()
        for row in rows:
            if row.effective_from >= start:
                raise CatalogueConflict("A rate starting at or after that moment already exists.")
            if row.effective_until is None or row.effective_until > start:
                row.effective_until = start
        await self._session.flush()
        row = ExchangeRate(
            base_currency=base,
            quote_currency=quote,
            rate=rate,
            effective_from=start,
            source=source,
            created_by=created_by,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
