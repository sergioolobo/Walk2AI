"""Administering the AI catalogue: a model reaches users only once it is checked,
priced from a cited source and enabled — and a price that may have charged a request
can never change afterwards."""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog
from app.services.ai.providers import FailureCode, ModelCheck
from tests.ai_support import HAIKU, LUNA, FakeProviders, prepare_catalogue, second_user
from tests.support import API, MutableClock, register_and_login

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


@pytest.fixture
async def admin(
    client: httpx.AsyncClient, session_factory: Sessions, fake_providers: FakeProviders
) -> httpx.AsyncClient:
    await prepare_catalogue(session_factory, enable=())
    await register_and_login(client)  # the first account is the administrator
    return client


async def new_model(admin: httpx.AsyncClient, identifier: str = "claude-test-1") -> dict[str, Any]:
    response = await admin.post(
        f"{API}/admin/models",
        json={
            "provider": "anthropic",
            "model_identifier": identifier,
            "display_name": "Claude Test",
            "supports_reasoning_effort": True,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def price(model_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "currency": "USD",
        "input_price": "1.00",
        "output_price": "5.00",
        "cached_input_price": "0.10",
        **extra,
    }


async def audit_actions(session_factory: Sessions) -> list[str]:
    """Catalogue audit entries, oldest first (sign-in entries left out)."""
    async with session_factory() as session:
        actions = (await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all()
    return [a for a in actions if not a.startswith("auth.")]


async def test_a_model_becomes_usable_only_after_check_verified_price_and_enabling(
    admin: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
) -> None:
    model = await new_model(admin)
    model_id = model["id"]
    assert model["enabled"] is False
    assert set(model["unavailable_reasons"]) >= {"disabled", "availability_not_checked", "no_price"}

    early = await admin.put(f"{API}/admin/models/{model_id}", json={"enabled": True})
    assert early.status_code == 409
    assert "available" in early.json()["detail"]

    fake_providers.anthropic.check = ModelCheck(
        available=True, context_limit=200_000, max_output_tokens=64_000
    )
    checked = await admin.post(f"{API}/admin/models/{model_id}/check")
    assert checked.status_code == 200, checked.text
    assert checked.json()["available"] is True
    assert checked.json()["model"]["context_limit"] == 200_000
    assert fake_providers.anthropic.checked == ["claude-test-1"]

    unverified = await admin.post(f"{API}/admin/model-pricing", json=price(model_id))
    assert unverified.status_code == 201, unverified.text
    still = await admin.put(f"{API}/admin/models/{model_id}", json={"enabled": True})
    assert still.status_code == 409
    assert "verified price" in still.json()["detail"]

    no_source = await admin.put(
        f"{API}/admin/model-pricing/{unverified.json()['id']}", json={"verified": True}
    )
    assert no_source.status_code == 422
    verified = await admin.put(
        f"{API}/admin/model-pricing/{unverified.json()['id']}",
        json={"verified": True, "source_url": SOURCE},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True and verified.json()["verified_at"] is not None

    enabled = await admin.put(f"{API}/admin/models/{model_id}", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert (enabled.json()["usable"], enabled.json()["unavailable_reasons"]) == (True, [])

    offered = (await admin.get(f"{API}/ai/models")).json()
    assert [m["model"] for m in offered] == ["claude-test-1"]
    assert offered[0]["prices"]["input"] == "1.00000000"
    assert await audit_actions(session_factory) == [
        "ai_model.create",
        "ai_model.check",
        "model_pricing.create",
        "model_pricing.update",
        "ai_model.update",
    ]


async def test_a_verified_price_in_force_is_never_changed_only_superseded(
    admin: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    model_id = (await new_model(admin))["id"]
    first = await admin.post(
        f"{API}/admin/model-pricing", json=price(model_id, verified=True, source_url=SOURCE)
    )
    assert first.status_code == 201

    edit = await admin.put(
        f"{API}/admin/model-pricing/{first.json()['id']}",
        json={"prices": {"input_price": "0.50", "output_price": "2.50"}},
    )
    assert edit.status_code == 409

    later = clock.now() + timedelta(days=1)
    second = await admin.post(
        f"{API}/admin/model-pricing",
        json=price(
            model_id,
            input_price="0.80",
            verified=True,
            source_url=SOURCE,
            effective_from=later.isoformat(),
        ),
    )
    assert second.status_code == 201, second.text

    versions = (await admin.get(f"{API}/admin/model-pricing", params={"model_id": model_id})).json()
    assert [v["input_price"] for v in versions] == ["0.80000000", "1.00000000"]
    assert versions[1]["effective_until"] == versions[0]["effective_from"]
    # A version scheduled for the future can still be corrected.
    corrected = await admin.put(
        f"{API}/admin/model-pricing/{second.json()['id']}", json={"notes": "announced"}
    )
    assert corrected.status_code == 200


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"effective_from": "2020-01-01T00:00:00Z"}, 422),  # cannot start in the past
        ({"verified": True}, 422),  # verified without a source
        ({"long_context_threshold": 100_000}, 422),  # a tier without its prices
        ({"input_price": "-1"}, 422),
        ({"input_price": 0.1}, 201),  # (a JSON number is accepted as a decimal)
        ({"currency": "usd"}, 422),
        ({"source_url": "http://insecure.example.com"}, 422),
    ],
)
async def test_malformed_prices_are_refused(
    admin: httpx.AsyncClient, body: dict[str, Any], status: int
) -> None:
    model_id = (await new_model(admin))["id"]

    response = await admin.post(f"{API}/admin/model-pricing", json=price(model_id, **body))

    assert response.status_code == status, response.text


async def test_a_model_found_unavailable_is_disabled(
    admin: httpx.AsyncClient, session_factory: Sessions, fake_providers: FakeProviders
) -> None:
    ids = await prepare_catalogue(session_factory, enable=(HAIKU,))
    fake_providers.anthropic.check = ModelCheck(available=False, detail="Not available.")

    checked = await admin.post(f"{API}/admin/models/{ids[HAIKU]}/check")

    assert checked.status_code == 200
    assert checked.json()["model"]["enabled"] is False
    assert "availability_not_checked" in checked.json()["model"]["unavailable_reasons"]


async def test_a_check_needs_a_configured_and_reachable_provider(
    fresh_app: FastAPI,
    admin: httpx.AsyncClient,
    session_factory: Sessions,
    fake_providers: FakeProviders,
) -> None:
    ids = await prepare_catalogue(session_factory, enable=())
    fake_providers.anthropic.check_failure = FailureCode.AUTH
    rejected = await admin.post(f"{API}/admin/models/{ids[HAIKU]}/check")
    fresh_app.state.ai_providers.pop("openai")
    unconfigured = await admin.post(f"{API}/admin/models/{ids[LUNA]}/check")

    assert rejected.status_code == 409
    assert rejected.json()["provider_code"] == FailureCode.AUTH
    assert unconfigured.status_code == 409
    assert unconfigured.json()["code"] == "provider_not_configured"


async def test_exchange_rates_are_versioned_and_audited(
    admin: httpx.AsyncClient, session_factory: Sessions, clock: MutableClock
) -> None:
    before = (await admin.get(f"{API}/admin/exchange-rates")).json()
    created = await admin.post(
        f"{API}/admin/exchange-rates",
        json={"base_currency": "USD", "quote_currency": "EUR", "rate": "0.91", "source": "ECB"},
    )
    clash = await admin.post(
        f"{API}/admin/exchange-rates",
        json={"base_currency": "USD", "quote_currency": "EUR", "rate": "0.95"},
    )
    same = await admin.post(
        f"{API}/admin/exchange-rates",
        json={"base_currency": "EUR", "quote_currency": "EUR", "rate": "1"},
    )

    assert created.status_code == 201, created.text
    assert created.json()["rate"] == "0.91000000"
    assert clash.status_code == 409  # one already starts at this very moment
    assert same.status_code == 422
    after = (await admin.get(f"{API}/admin/exchange-rates")).json()
    usd = [r for r in after if r["base_currency"] == "USD"]
    # The test catalogue's rate (from 2026-01-01) now ends where the new one starts.
    assert [r["rate"] for r in usd] == ["0.91000000", "0.92000000"]
    assert usd[1]["effective_until"] == usd[0]["effective_from"]
    assert len(after) == len(before) + 1
    assert "exchange_rate.create" in await audit_actions(session_factory)


async def test_catalogue_administration_requires_an_administrator(
    fresh_app: FastAPI, admin: httpx.AsyncClient, make_client: Any
) -> None:
    async with make_client() as other:
        await second_user(fresh_app, other)
        for method, path in [
            ("GET", "/admin/models"),
            ("POST", "/admin/models"),
            ("PUT", f"/admin/models/{uuid.uuid4()}"),
            ("POST", f"/admin/models/{uuid.uuid4()}/check"),
            ("GET", "/admin/model-pricing"),
            ("POST", "/admin/model-pricing"),
            ("PUT", f"/admin/model-pricing/{uuid.uuid4()}"),
            ("GET", "/admin/exchange-rates"),
            ("POST", "/admin/exchange-rates"),
        ]:
            response = await other.request(method, f"{API}{path}", json={})
            assert response.status_code == 403, (method, path)


async def test_the_admin_list_explains_every_model(
    admin: httpx.AsyncClient, session_factory: Sessions
) -> None:
    listed = {m["model_identifier"]: m for m in (await admin.get(f"{API}/admin/models")).json()}

    assert set(listed) == {HAIKU, "claude-sonnet-5-5", "claude-opus-5-5", LUNA}
    assert listed[LUNA]["unavailable_reasons"] == ["disabled", "availability_not_checked"]
    assert listed[HAIKU]["unavailable_reasons"] == ["disabled", "availability_not_checked"]
