"""Device pairing and scoped device-token authentication."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import AppFactory, ClientFactory
from tests.support import API, MutableClock, register, register_and_login

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


async def new_pairing_code(client: httpx.AsyncClient) -> str:
    response = await client.post(f"{API}/auth/devices/pairing-code")
    assert response.status_code == 201, response.text
    code: str = response.json()["code"]
    return code


async def pair(http: httpx.AsyncClient, code: str, name: str = "iPhone 17 Pro") -> httpx.Response:
    return await http.post(f"{API}/auth/devices/pair", json={"code": code, "name": name})


async def paired_device(client: httpx.AsyncClient, make_client: ClientFactory) -> tuple[str, str]:
    """Pair a device from a separate, cookie-less client. Returns (device id, token)."""
    code = await new_pairing_code(client)
    async with make_client() as phone:
        response = await pair(phone, code)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["device"]["id"], body["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_pairing_flow_end_to_end(
    client: httpx.AsyncClient,
    make_client: ClientFactory,
    clock: MutableClock,
    session_factory: Sessions,
) -> None:
    account = await register_and_login(client)
    created = await client.post(f"{API}/auth/devices/pairing-code")
    code = created.json()["code"]

    assert created.status_code == 201
    assert len(code) == 9 and code[4] == "-"  # e.g. "ABCD-EFGH"
    assert created.json()["expires_at"].startswith("2026-11-13T12:10:00")  # ten minutes

    async with make_client() as phone:  # the phone has no cookies and no password
        response = await pair(phone, code)
        assert response.status_code == 201
        body = response.json()
        token = body["token"]
        assert token.startswith("w2a_dev_")
        assert body["device"]["name"] == "iPhone 17 Pro"
        assert body["device"]["scopes"] == ["fitness:sync"]
        assert body["device"]["last_used_at"] is None

        clock.advance(minutes=3)
        current = await phone.get(f"{API}/auth/devices/current", headers=bearer(token))

    assert current.status_code == 200
    assert current.json()["user_id"] == str(account.id)
    assert current.json()["timezone"] == "Europe/Lisbon"
    assert current.json()["device"]["last_used_at"].startswith("2026-11-13T12:03:00")

    listed = (await client.get(f"{API}/auth/devices")).json()
    assert [d["name"] for d in listed] == ["iPhone 17 Pro"]
    assert "token" not in listed[0]  # the token is shown once, at pairing, never again

    async with session_factory() as session:
        dump = str((await session.execute(text("SELECT * FROM device_tokens"))).all())
        codes = str((await session.execute(text("SELECT * FROM device_pairing_codes"))).all())
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY id"))).all()
    assert token not in dump
    assert code.replace("-", "") not in codes
    assert actions[-2:] == ["device.pairing_code_created", "device.paired"]


async def test_pairing_code_requires_a_signed_in_user(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"{API}/auth/devices/pairing-code")).status_code == 401
    assert (await client.get(f"{API}/auth/devices")).status_code == 401


async def test_pairing_code_is_forgiving_about_formatting(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    await register_and_login(client)
    code = await new_pairing_code(client)
    async with make_client() as phone:
        response = await pair(phone, f" {code.replace('-', '').lower()} ")
    assert response.status_code == 201


async def test_pairing_code_works_exactly_once(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    await register_and_login(client)
    code = await new_pairing_code(client)
    async with make_client() as phone:
        first = await pair(phone, code)
        second = await pair(phone, code, name="Another phone")

    assert first.status_code == 201
    assert second.status_code == 401
    assert second.json()["code"] == "invalid_pairing_code"
    assert len((await client.get(f"{API}/auth/devices")).json()) == 1


async def test_only_one_of_many_simultaneous_pairings_succeeds(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register_and_login(client)
    code = await new_pairing_code(client)

    async def attempt(index: int) -> int:
        async with make_client() as phone:
            return (await pair(phone, code, name=f"phone {index}")).status_code

    statuses = await asyncio.gather(*(attempt(i) for i in range(8)))

    assert sorted(statuses) == [201] + [401] * 7
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM device_tokens")) == 1


async def test_pairing_code_expires(
    client: httpx.AsyncClient, make_client: ClientFactory, clock: MutableClock
) -> None:
    await register_and_login(client)
    code = await new_pairing_code(client)
    clock.advance(minutes=10, seconds=1)
    async with make_client() as phone:
        response = await pair(phone, code)
    assert response.status_code == 401


async def test_a_new_pairing_code_invalidates_the_previous_one(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    await register_and_login(client)
    old_code = await new_pairing_code(client)
    new_code = await new_pairing_code(client)
    async with make_client() as phone:
        assert (await pair(phone, old_code)).status_code == 401
        assert (await pair(phone, new_code)).status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "ZZZZ-ZZZZ", "name": "phone"},  # well-formed but wrong
        {"code": "short", "name": "phone"},
        {"code": "ABCD-EFGH"},  # no name
        {"code": "ABCD-EFGH", "name": "phone", "scopes": ["admin"]},  # cannot choose scopes
        {"code": "ABCD-EFGH", "name": "phone", "platform": "Not Valid!"},
    ],
)
async def test_invalid_pairing_requests_are_refused(
    client: httpx.AsyncClient, make_client: ClientFactory, payload: dict[str, object]
) -> None:
    await register_and_login(client)
    await new_pairing_code(client)
    async with make_client() as phone:
        response = await phone.post(f"{API}/auth/devices/pair", json=payload)
    assert response.status_code in (401, 422)
    assert (await client.get(f"{API}/auth/devices")).json() == []


async def test_pairing_attempts_are_rate_limited(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    """The code is short; the attempt limit is what makes guessing it hopeless."""
    await register_and_login(client)
    code = await new_pairing_code(client)
    async with make_client() as attacker:
        statuses = [(await pair(attacker, f"GUES-S{i:03d}")).status_code for i in range(10)]
        blocked = await pair(attacker, "GUES-S999")
        # Even the right code is refused once the limit is hit.
        right_code = await pair(attacker, code)

    assert statuses == [401] * 10
    assert blocked.status_code == 429
    assert right_code.status_code == 429


async def test_device_token_cannot_reach_anything_but_its_scope(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    """The phone's token syncs fitness data. It is not a login."""
    await register_and_login(client)  # first account: an administrator
    _, token = await paired_device(client, make_client)

    async with make_client() as phone:
        headers = bearer(token)
        refused = {
            "GET /auth/me": await phone.get(f"{API}/auth/me", headers=headers),
            "PATCH /auth/me": await phone.patch(
                f"{API}/auth/me", headers=headers, json={"display_name": "x"}
            ),
            "GET /wallet": await phone.get(f"{API}/wallet", headers=headers),
            "GET /wallet/history": await phone.get(f"{API}/wallet/history", headers=headers),
            "GET /auth/devices": await phone.get(f"{API}/auth/devices", headers=headers),
            "POST pairing-code": await phone.post(
                f"{API}/auth/devices/pairing-code", headers=headers
            ),
            "POST adjustments": await phone.post(
                f"{API}/admin/wallet/adjustments",
                headers=headers,
                json={"user_id": str(uuid.uuid4()), "amount_wc": 100000, "reason": "free credits"},
            ),
            "GET reconcile": await phone.get(f"{API}/admin/wallet/reconcile", headers=headers),
            "GET audit-log": await phone.get(f"{API}/admin/audit-log", headers=headers),
        }
        # Nor does presenting the device token as if it were a session cookie work.
        phone.cookies.set("w2a_access", token, domain="testserver.local", path="/")
        as_cookie = await phone.get(f"{API}/auth/me")

    assert {name: r.status_code for name, r in refused.items()} == dict.fromkeys(refused, 401)
    assert as_cookie.status_code == 401
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 0


async def test_a_browser_session_is_not_a_device_token(client: httpx.AsyncClient) -> None:
    await register_and_login(client)
    access = client.cookies.get("w2a_access")
    assert access is not None

    with_cookie_only = await client.get(f"{API}/auth/devices/current")
    with_jwt_as_bearer = await client.get(f"{API}/auth/devices/current", headers=bearer(access))

    assert with_cookie_only.status_code == 401
    assert with_cookie_only.headers["www-authenticate"] == "Bearer"
    assert with_jwt_as_bearer.status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "w2a_dev_without_scheme",
        "Bearer w2a_dev_this-token-does-not-exist",
        "Bearer not-even-the-right-prefix",
    ],
)
async def test_malformed_or_unknown_device_credentials_are_rejected(
    client: httpx.AsyncClient, header: str
) -> None:
    headers = {"Authorization": header} if header else {}
    response = await client.get(f"{API}/auth/devices/current", headers=headers)
    assert response.status_code == 401


async def test_revoking_a_device_stops_its_token_immediately(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register_and_login(client)
    device_id, token = await paired_device(client, make_client)

    async with make_client() as phone:
        assert (
            await phone.get(f"{API}/auth/devices/current", headers=bearer(token))
        ).status_code == 200

        revoked = await client.delete(f"{API}/auth/devices/{device_id}")
        again = await client.delete(f"{API}/auth/devices/{device_id}")  # idempotent

        assert (revoked.status_code, again.status_code) == (204, 204)
        assert (
            await phone.get(f"{API}/auth/devices/current", headers=bearer(token))
        ).status_code == 401

    listed = (await client.get(f"{API}/auth/devices")).json()
    assert listed[0]["revoked_at"] is not None
    async with session_factory() as session:
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY id"))).all()
    assert actions.count("device.revoked") == 1


async def test_a_user_cannot_see_or_revoke_someone_elses_device(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as owner,
        make_client(app) as stranger,
        make_client(app) as phone,
    ):
        await register_and_login(owner, "owner@example.com")
        await register_and_login(stranger, "stranger@example.com")
        code = (await owner.post(f"{API}/auth/devices/pairing-code")).json()["code"]
        device = (await pair(phone, code)).json()

        assert (await stranger.get(f"{API}/auth/devices")).json() == []
        response = await stranger.delete(f"{API}/auth/devices/{device['device']['id']}")
        assert response.status_code == 404
        still_works = await phone.get(
            f"{API}/auth/devices/current", headers=bearer(device["token"])
        )
        assert still_works.status_code == 200
        unknown = await owner.delete(f"{API}/auth/devices/{uuid.uuid4()}")
        assert unknown.status_code == 404


async def test_device_token_stops_working_when_the_account_is_disabled(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register_and_login(client)
    _, token = await paired_device(client, make_client)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE users SET account_status = 'suspended'"))

    async with make_client() as phone:
        response = await phone.get(f"{API}/auth/devices/current", headers=bearer(token))
    assert response.status_code == 401


async def test_a_token_without_the_required_scope_is_forbidden(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register_and_login(client)
    _, token = await paired_device(client, make_client)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE device_tokens SET scopes = ARRAY['something:else']"))

    async with make_client() as phone:
        response = await phone.get(f"{API}/auth/devices/current", headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_scope"


async def test_a_pairing_code_for_a_disabled_account_is_useless(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register(client)
    await register_and_login_existing(client)
    code = await new_pairing_code(client)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE users SET account_status = 'suspended'"))
    async with make_client() as phone:
        assert (await pair(phone, code)).status_code == 401


async def register_and_login_existing(client: httpx.AsyncClient) -> None:
    from tests.support import DEFAULT_PASSWORD

    response = await client.post(
        f"{API}/auth/login", json={"email": "owner@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200
