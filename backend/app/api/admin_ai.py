"""Administration of the AI catalogue: models, prices and conversion rates.

Every route requires the admin role; every write is audited. A model can be offered to
users only once it is enabled, confirmed available to this account, and has a verified
price in force (and the conversion rates it needs exist).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import AdminDep, ClockDep, RequestMetaDep, SessionDep, require_admin
from app.core.errors import Conflict
from app.models.ai import AIModel, ExchangeRate, ModelPricing
from app.models.enums import ActorType
from app.schemas.ai import (
    AdminModelResponse,
    ModelCheckResponse,
    ModelCreate,
    ModelUpdate,
    PriceCreate,
    PriceFields,
    PriceResponse,
    PriceUpdate,
    RateCreate,
    RateResponse,
)
from app.services.ai import catalogue
from app.services.ai.catalogue import CatalogueAdmin, PriceInput
from app.services.ai.providers import ProviderAdapter, ProviderFailure
from app.services.audit import record_audit

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def get_catalogue_admin(session: SessionDep, clock: ClockDep) -> CatalogueAdmin:
    return CatalogueAdmin(session, clock)


CatalogueDep = Annotated[CatalogueAdmin, Depends(get_catalogue_admin)]


class ProviderNotConfigured(Conflict):
    code = "provider_not_configured"
    title = "No API key is configured for this provider"


def _providers(request: Request) -> dict[str, ProviderAdapter]:
    providers: dict[str, ProviderAdapter] = request.app.state.ai_providers
    return providers


async def _model_response(
    session: SessionDep, request: Request, clock: ClockDep, model: AIModel
) -> AdminModelResponse:
    status_ = await catalogue.status_of(session, model, _providers(request).keys(), clock.now())
    return AdminModelResponse(
        id=model.id,
        provider=model.provider,
        model_identifier=model.model_identifier,
        display_name=model.display_name,
        context_limit=model.context_limit,
        max_output_tokens=model.max_output_tokens,
        supports_streaming=model.supports_streaming,
        supports_temperature=model.supports_temperature,
        supports_reasoning_effort=model.supports_reasoning_effort,
        enabled=model.enabled,
        availability_checked_at=model.availability_checked_at,
        sort_order=model.sort_order,
        notes=model.notes,
        usable=status_.usable,
        unavailable_reasons=list(status_.reasons),
        price_id=status_.price.id if status_.price else None,
    )


def _price_response(row: ModelPricing) -> PriceResponse:
    return PriceResponse.model_validate(row, from_attributes=True)


def _price_input(fields: PriceFields) -> PriceInput:
    return PriceInput(**fields.model_dump(include=set(PriceFields.model_fields)))


def _price_details(row: ModelPricing) -> dict[str, Any]:
    return {
        "model_id": str(row.model_id),
        "currency": row.currency,
        "input_price": format(row.input_price, "f"),
        "output_price": format(row.output_price, "f"),
        "effective_from": row.effective_from.isoformat(),
        "verified": row.verified,
        "source_url": row.source_url,
    }


# ---------------------------------------------------------------------- models


@router.get("/models", response_model=list[AdminModelResponse])
async def list_models(
    request: Request, session: SessionDep, clock: ClockDep
) -> list[AdminModelResponse]:
    """Every model in the catalogue, and why each one is not usable, if it is not."""
    statuses = await catalogue.list_statuses(session, _providers(request).keys(), clock.now())
    return [await _model_response(session, request, clock, s.model) for s in statuses]


@router.post("/models", response_model=AdminModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreate,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> AdminModelResponse:
    """Add a model. It starts disabled."""
    model = await admin_catalogue.create_model(**body.model_dump())
    record_audit(
        session,
        clock,
        action="ai_model.create",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="ai_model",
        entity_id=model.id,
        details={"provider": model.provider, "model": model.model_identifier},
        meta=meta,
    )
    await session.commit()
    return await _model_response(session, request, clock, model)


@router.put("/models/{model_id}", response_model=AdminModelResponse)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> AdminModelResponse:
    """Change a model's settings, or enable/disable it. Enabling requires a successful
    availability check and a verified price in force."""
    changes = body.model_dump(include=body.model_fields_set)
    model = await admin_catalogue.update_model(model_id, changes)
    record_audit(
        session,
        clock,
        action="ai_model.update",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="ai_model",
        entity_id=model.id,
        details={"changed": {k: str(v) for k, v in changes.items()}},
        meta=meta,
    )
    await session.commit()
    return await _model_response(session, request, clock, model)


@router.post("/models/{model_id}/check", response_model=ModelCheckResponse)
async def check_model(
    model_id: uuid.UUID,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> ModelCheckResponse:
    """Ask the provider whether this API account can use the model.

    This reads the provider's model metadata: it generates nothing and costs nothing.
    A model found unavailable is disabled.
    """
    model = await admin_catalogue.get_model(model_id)
    adapter = _providers(request).get(model.provider)
    if adapter is None:
        raise ProviderNotConfigured(
            f"Set the {model.provider} API key in the server's environment first."
        )
    try:
        check = await adapter.check_model(model.model_identifier)
    except ProviderFailure as failure:
        raise Conflict(
            f"The provider could not be asked: {failure.user_message}",
            extra={"provider_code": failure.code.value},
        ) from failure
    await admin_catalogue.record_check(model, check)
    record_audit(
        session,
        clock,
        action="ai_model.check",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="ai_model",
        entity_id=model.id,
        details={"available": check.available, "detail": check.detail},
        meta=meta,
    )
    await session.commit()
    return ModelCheckResponse(
        available=check.available,
        detail=check.detail,
        model=await _model_response(session, request, clock, model),
    )


# ---------------------------------------------------------------------- prices


@router.get("/model-pricing", response_model=list[PriceResponse])
async def list_prices(
    admin_catalogue: CatalogueDep, model_id: Annotated[uuid.UUID | None, Query()] = None
) -> list[PriceResponse]:
    """Every price version (per million tokens), newest first per model."""
    return [_price_response(row) for row in await admin_catalogue.list_prices(model_id)]


@router.post("/model-pricing", response_model=PriceResponse, status_code=status.HTTP_201_CREATED)
async def create_price(
    body: PriceCreate,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> PriceResponse:
    """Publish a price version from now (or later). The version in force ends where this
    one starts; requests already charged keep the price they were charged at."""
    row = await admin_catalogue.create_price(
        body.model_id,
        _price_input(body),
        effective_from=body.effective_from,
        verified=body.verified,
        source_url=body.source_url,
        notes=body.notes,
        created_by=admin.user.id,
    )
    record_audit(
        session,
        clock,
        action="model_pricing.create",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="model_pricing",
        entity_id=row.id,
        details=_price_details(row),
        meta=meta,
    )
    await session.commit()
    return _price_response(row)


@router.put("/model-pricing/{price_id}", response_model=PriceResponse)
async def update_price(
    price_id: uuid.UUID,
    body: PriceUpdate,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> PriceResponse:
    """Correct an unverified price, or mark it verified (citing the provider's page).
    A verified price in force cannot change: publish a new version instead."""
    row = await admin_catalogue.update_price(
        price_id,
        prices=_price_input(body.prices) if body.prices is not None else None,
        verify=body.verified is True,
        source_url=body.source_url,
        notes=body.notes,
        fields=frozenset(body.model_fields_set),
    )
    record_audit(
        session,
        clock,
        action="model_pricing.update",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="model_pricing",
        entity_id=row.id,
        details={"changed": sorted(body.model_fields_set), **_price_details(row)},
        meta=meta,
    )
    await session.commit()
    return _price_response(row)


# ----------------------------------------------------------------------- rates


@router.get("/exchange-rates", response_model=list[RateResponse])
async def list_rates(admin_catalogue: CatalogueDep) -> list[ExchangeRate]:
    """Every conversion-rate version: currency → EUR, and EUR → Walk Credits."""
    return list(await admin_catalogue.list_rates())


@router.post("/exchange-rates", response_model=RateResponse, status_code=status.HTTP_201_CREATED)
async def create_rate(
    body: RateCreate,
    admin: AdminDep,
    session: SessionDep,
    admin_catalogue: CatalogueDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> ExchangeRate:
    """Set a conversion rate from now (or later), e.g. 1 USD = 0.92 EUR. Requests already
    charged keep the rate they were charged at."""
    row = await admin_catalogue.create_rate(
        body.base_currency,
        body.quote_currency,
        body.rate,
        effective_from=body.effective_from,
        source=body.source,
        created_by=admin.user.id,
    )
    record_audit(
        session,
        clock,
        action="exchange_rate.create",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="exchange_rate",
        entity_id=row.id,
        details={
            "base": row.base_currency,
            "quote": row.quote_currency,
            "rate": format(row.rate, "f"),
            "effective_from": row.effective_from.isoformat(),
            "source": row.source,
        },
        meta=meta,
    )
    await session.commit()
    return row
