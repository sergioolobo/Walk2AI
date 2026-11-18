"""Cross-cutting HTTP behaviour: probes, errors, headers, CSRF protection, docs."""

import logging

import httpx
import pytest
from fastapi import APIRouter
from pydantic import SecretStr

from tests.conftest import AppFactory, ClientFactory
from tests.support import API, DEFAULT_PASSWORD, register, register_and_login

pytestmark = pytest.mark.anyio

PROBLEM_JSON = "application/problem+json"


# ------------------------------------------------------------------------ probes


async def test_liveness_and_readiness(client: httpx.AsyncClient) -> None:
    live = await client.get(f"{API}/health")
    ready = await client.get(f"{API}/health/ready")

    assert (live.status_code, live.json()) == (200, {"status": "ok"})
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": True, "schema_current": True}


async def test_readiness_fails_when_the_schema_is_not_the_expected_revision(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    """A build that expects a newer migration must not report itself ready."""
    async with make_app() as app, make_client(app) as http:
        app.state.schema_head = "9999"
        response = await http.get(f"{API}/health/ready")
        assert (await http.get(f"{API}/health")).status_code == 200  # still alive
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": True, "schema_current": False}


async def test_readiness_fails_when_the_database_is_unreachable(
    make_app: AppFactory, make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    unreachable = SecretStr(
        "postgresql+psycopg://nobody:wrong@127.0.0.1:1/absent?connect_timeout=2"
    )
    async with make_app(database_url=unreachable) as app, make_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="walk2ai.health"):
            response = await http.get(f"{API}/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": False, "schema_current": False}


# ----------------------------------------------------------------------- headers


async def test_every_response_carries_a_request_id_and_defensive_headers(
    client: httpx.AsyncClient,
) -> None:
    for response in (
        await client.get(f"{API}/health"),
        await client.get(f"{API}/auth/me"),  # 401
        await client.get(f"{API}/does-not-exist"),  # 404
    ):
        assert len(response.headers["x-request-id"]) >= 8
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        # Balances and personal data must never be cached by a browser or a proxy.
        assert response.headers["cache-control"] == "no-store"


async def test_request_id_is_propagated_when_valid_and_replaced_when_not(
    client: httpx.AsyncClient,
) -> None:
    kept = await client.get(f"{API}/auth/me", headers={"X-Request-ID": "trace-abc-12345"})
    replaced = await client.get(f"{API}/auth/me", headers={"X-Request-ID": "bad id\twith spaces"})
    too_long = await client.get(f"{API}/auth/me", headers={"X-Request-ID": "x" * 200})

    assert kept.headers["x-request-id"] == "trace-abc-12345"
    assert kept.json()["request_id"] == "trace-abc-12345"  # errors quote it for support
    assert replaced.headers["x-request-id"] != "bad id\twith spaces"
    assert len(too_long.headers["x-request-id"]) == 32
    assert kept.headers["x-request-id"] != replaced.headers["x-request-id"]


# ------------------------------------------------------------------------ errors


async def test_errors_are_problem_json_with_stable_codes(client: httpx.AsyncClient) -> None:
    cases = {
        "not_found": await client.get(f"{API}/does-not-exist"),
        "method_not_allowed": await client.delete(f"{API}/health"),
        "not_authenticated": await client.get(f"{API}/wallet"),
        "validation_error": await client.post(f"{API}/auth/login", json={"email": "x"}),
    }
    for code, response in cases.items():
        body = response.json()
        assert response.headers["content-type"] == PROBLEM_JSON, code
        assert body["code"] == code
        assert body["status"] == response.status_code
        assert {"type", "title", "status", "detail", "code", "request_id"} <= set(body)


async def test_malformed_json_is_a_validation_error_not_a_crash(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"{API}/auth/login",
        content=b'{"email": "owner@example.com", "password": ',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_unhandled_exceptions_become_a_generic_500_that_leaks_nothing(
    make_app: AppFactory, make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    crashing = APIRouter()

    @crashing.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail: connection string postgres://user:hunter2@db")

    async with make_app() as app, make_client(app) as http:
        app.include_router(crashing, prefix=API)
        with caplog.at_level(logging.ERROR, logger="walk2ai.errors"):
            response = await http.get(f"{API}/boom", headers={"X-Request-ID": "crash-trace-0001"})

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == "crash-trace-0001"
    assert response.headers["x-request-id"] == "crash-trace-0001"
    # The client learns nothing about the failure...
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    # ...but the operator's log has the full story.
    assert any("unhandled exception" in record.message for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)


# -------------------------------------------------------------------------- CSRF


async def test_cross_origin_state_changes_are_rejected(client: httpx.AsyncClient) -> None:
    """A page on another site cannot make the browser act with the user's cookies."""
    account = await register_and_login(client)

    foreign_origin = await client.patch(
        f"{API}/auth/me",
        json={"display_name": "pwned"},
        headers={"Origin": "https://evil.example"},
    )
    cross_site = await client.post(f"{API}/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    lookalike = await client.patch(
        f"{API}/auth/me",
        json={"display_name": "pwned"},
        headers={"Origin": "http://testserver.evil.example"},
    )

    for response in (foreign_origin, cross_site, lookalike):
        assert response.status_code == 403
        assert response.json()["code"] == "cross_origin_rejected"
    me = await client.get(f"{API}/auth/me")
    assert me.status_code == 200  # not logged out, not renamed
    assert me.json()["display_name"] is None
    assert me.json()["id"] == str(account.id)


async def test_same_origin_requests_and_safe_methods_pass(client: httpx.AsyncClient) -> None:
    await register_and_login(client)

    same_origin = await client.patch(
        f"{API}/auth/me",
        json={"display_name": "Owner"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    # Reads are safe: they change nothing, so a foreign Origin header is irrelevant.
    read = await client.get(f"{API}/auth/me", headers={"Origin": "https://evil.example"})

    assert same_origin.status_code == 200
    assert read.status_code == 200


async def test_token_authenticated_clients_are_not_subject_to_the_origin_check(
    client: httpx.AsyncClient,
) -> None:
    """The mobile companion sends an Authorization header, not ambient cookies."""
    response = await client.post(
        f"{API}/auth/devices/pair",
        json={"code": "ABCD-EFGH", "name": "phone"},
        headers={"Authorization": "Bearer anything", "Origin": "https://evil.example"},
    )
    assert response.status_code == 401  # refused for the bad code, not as cross-origin
    assert response.json()["code"] == "invalid_pairing_code"


# -------------------------------------------------------------------------- docs


async def test_api_documentation_is_served_outside_production(client: httpx.AsyncClient) -> None:
    spec = await client.get(f"{API}/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert f"{API}/wallet" in paths
    assert f"{API}/auth/login" in paths
    assert (await client.get(f"{API}/docs")).status_code == 200


async def test_api_documentation_is_disabled_in_production(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(
            environment="production", cookie_secure=True, public_origin="https://walk2ai.example"
        ) as app,
        make_client(app, base_url="https://testserver") as http,
    ):
        assert (await http.get(f"{API}/openapi.json")).status_code == 404
        assert (await http.get(f"{API}/docs")).status_code == 404
        assert (await http.get(f"{API}/health")).status_code == 200


# ----------------------------------------------------------------------- secrets


async def test_provider_keys_and_server_secrets_never_reach_a_client(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    """Whatever a client asks for, configured secrets stay on the server."""
    openai_key = "sk-test-openai-DO-NOT-LEAK-123456"
    anthropic_key = "sk-ant-test-DO-NOT-LEAK-654321"
    async with (
        make_app(
            openai_api_key=SecretStr(openai_key), anthropic_api_key=SecretStr(anthropic_key)
        ) as app,
        make_client(app) as http,
    ):
        account = await register(http)
        responses = [
            await http.post(
                f"{API}/auth/login", json={"email": account.email, "password": DEFAULT_PASSWORD}
            ),
            await http.get(f"{API}/auth/me"),
            await http.get(f"{API}/wallet"),
            await http.get(f"{API}/wallet/summary"),
            await http.get(f"{API}/admin/audit-log"),
            await http.get(f"{API}/admin/wallet/reconcile"),
            await http.get(f"{API}/health/ready"),
            await http.get(f"{API}/openapi.json"),
            await http.get(f"{API}/does-not-exist"),
        ]
        jwt_secret = app.state.settings.jwt_secret.get_secret_value()
        database_password = app.state.settings.sqlalchemy_url.password

    assert database_password
    for response in responses:
        exposed = response.text + str(response.headers)
        for secret in (openai_key, anthropic_key, jwt_secret, database_password):
            assert secret not in exposed
