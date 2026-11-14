"""Authentication and account management over HTTP."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import AppFactory, ClientFactory
from tests.support import (
    API,
    DEFAULT_PASSWORD,
    MutableClock,
    login,
    register,
    register_and_login,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


def copy_cookies(source: httpx.AsyncClient, target: httpx.AsyncClient) -> None:
    """Give ``target`` a copy of ``source``'s cookies (a stolen or stale cookie jar)."""
    for cookie in source.cookies.jar:
        target.cookies.jar.set_cookie(cookie)


async def audit_actions(session_factory: Sessions) -> list[str]:
    async with session_factory() as session:
        rows = await session.scalars(text("SELECT action FROM audit_log ORDER BY id"))
        return list(rows.all())


# ------------------------------------------------------------------ registration


async def test_first_account_becomes_the_administrator(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    response = await client.post(
        f"{API}/auth/register",
        json={"email": "owner@example.com", "password": DEFAULT_PASSWORD, "display_name": "Owner"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "admin"
    assert body["email"] == "owner@example.com"
    assert body["display_name"] == "Owner"
    assert body["timezone"] == "Europe/Lisbon"  # the application default
    assert set(body) == {"id", "email", "display_name", "role", "timezone", "created_at"}
    assert "set-cookie" not in response.headers  # registering does not sign in

    async with session_factory() as session:
        stored_hash = await session.scalar(text("SELECT password_hash FROM users"))
        wallet = (
            await session.execute(
                text("SELECT available_balance, reserved_balance FROM wallet_accounts")
            )
        ).one()
        limits = await session.scalar(text("SELECT count(*) FROM spending_limits"))
    assert stored_hash.startswith("$argon2id$")
    assert DEFAULT_PASSWORD not in stored_hash
    assert wallet == (0, 0)  # every account starts with an empty wallet
    assert limits == 1
    assert await audit_actions(session_factory) == ["auth.register"]


async def test_registration_closes_after_the_first_account(client: httpx.AsyncClient) -> None:
    await register(client)
    response = await client.post(
        f"{API}/auth/register", json={"email": "second@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "registration_closed"


async def test_closed_mode_refuses_even_the_first_account(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with make_app(registration_mode="closed") as app, make_client(app) as http:
        response = await http.post(
            f"{API}/auth/register",
            json={"email": "owner@example.com", "password": DEFAULT_PASSWORD},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "registration_closed"


async def test_open_mode_adds_ordinary_users_and_rejects_duplicate_emails(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with make_app(registration_mode="open") as app, make_client(app) as http:
        first = await register(http, "owner@example.com")
        second = await register(http, "friend@example.com")
        duplicate = await http.post(
            f"{API}/auth/register",
            json={"email": "FRIEND@example.com", "password": DEFAULT_PASSWORD},
        )
    assert (first.role, second.role) == ("admin", "user")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "email_taken"


async def test_only_one_of_many_simultaneous_registrations_becomes_admin(
    make_client: ClientFactory, session_factory: Sessions
) -> None:
    async def attempt(index: int) -> int:
        async with make_client() as http:
            response = await http.post(
                f"{API}/auth/register",
                json={"email": f"racer{index}@example.com", "password": DEFAULT_PASSWORD},
            )
            return response.status_code

    statuses = await asyncio.gather(*(attempt(i) for i in range(10)))

    assert sorted(statuses) == [201] + [403] * 9
    async with session_factory() as session:
        roles = (await session.scalars(text("SELECT role FROM users"))).all()
        wallets = await session.scalar(text("SELECT count(*) FROM wallet_accounts"))
    assert roles == ["admin"]
    assert wallets == 1


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"email": "owner@example.com", "password": "Zq7!pw"}, "password"),
        ({"email": "owner@example.com", "password": "x" * 129}, "password"),
        ({"email": "not-an-email", "password": DEFAULT_PASSWORD}, "email"),
        ({"email": "owner@example.com"}, "password"),
        ({"email": "owner@example.com", "password": DEFAULT_PASSWORD, "role": "admin"}, "role"),
        (
            {"email": "owner@example.com", "password": DEFAULT_PASSWORD, "display_name": "x" * 81},
            "display_name",
        ),
    ],
)
async def test_invalid_registration_is_rejected_without_echoing_input(
    client: httpx.AsyncClient, session_factory: Sessions, payload: dict[str, str], field: str
) -> None:
    response = await client.post(f"{API}/auth/register", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "validation_error"
    assert field in {error["field"] for error in body["errors"]}
    # A rejected payload may contain a password: it must never come back.
    assert payload.get("password", "\x00") not in response.text
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM users")) == 0


# ------------------------------------------------------------------------- login


async def test_login_sets_http_only_cookies_and_returns_no_token(
    client: httpx.AsyncClient,
) -> None:
    account = await register(client)
    response = await login(client, account)

    assert response.json()["email"] == account.email
    assert "token" not in response.text.lower()
    cookies = {c.split("=", 1)[0]: c for c in response.headers.get_list("set-cookie")}
    assert set(cookies) == {"w2a_access", "w2a_refresh"}
    access, refresh = cookies["w2a_access"].lower(), cookies["w2a_refresh"].lower()
    assert "httponly" in access and "httponly" in refresh
    assert "samesite=lax" in access and "path=/;" in access + ";"
    assert "samesite=strict" in refresh
    # The refresh token is only ever sent to the auth endpoints.
    assert "path=/api/v1/auth" in refresh
    assert "max-age=900" in access  # 15 minutes
    assert "max-age=2592000" in refresh  # 30 days


async def test_cookies_are_marked_secure_when_configured(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with (
        make_app(cookie_secure=True) as app,
        make_client(app, base_url="https://testserver") as http,
    ):
        account = await register(http)
        response = await login(http, account)
    for cookie in response.headers.get_list("set-cookie"):
        assert "secure" in cookie.lower()


async def test_wrong_password_and_unknown_account_are_indistinguishable(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    account = await register(client)
    wrong_password = await client.post(
        f"{API}/auth/login", json={"email": account.email, "password": "definitely not it"}
    )
    unknown_account = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD}
    )

    for response in (wrong_password, unknown_account):
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
    strip = {"request_id"}
    assert {k: v for k, v in wrong_password.json().items() if k not in strip} == {
        k: v for k, v in unknown_account.json().items() if k not in strip
    }
    assert wrong_password.json()["code"] == "invalid_credentials"
    # Both failures are on the audit trail, even though the requests "failed".
    assert await audit_actions(session_factory) == [
        "auth.register",
        "auth.login_failed",
        "auth.login_failed",
    ]


async def test_email_is_case_insensitive_but_password_whitespace_is_significant(
    client: httpx.AsyncClient,
) -> None:
    password = "  spaces are part of this password  "
    await register(client, "owner@example.com", password)

    exact = await client.post(
        f"{API}/auth/login", json={"email": "OWNER@Example.com", "password": password}
    )
    trimmed = await client.post(
        f"{API}/auth/login", json={"email": "owner@example.com", "password": password.strip()}
    )
    assert exact.status_code == 200
    assert trimmed.status_code == 401


async def test_repeated_failed_logins_are_rate_limited_per_account(
    client: httpx.AsyncClient,
) -> None:
    account = await register(client)
    attempt = {"email": account.email, "password": "wrong password guess"}
    statuses = [
        (await client.post(f"{API}/auth/login", json=attempt)).status_code for _ in range(10)
    ]
    blocked = await client.post(f"{API}/auth/login", json=attempt)
    # Even the right password is refused while the account is cooling down.
    still_blocked = await client.post(
        f"{API}/auth/login", json={"email": account.email, "password": account.password}
    )

    assert statuses == [401] * 10
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) > 0
    assert still_blocked.status_code == 429


async def test_rate_limiting_can_be_disabled_by_configuration(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    async with make_app(rate_limit_enabled=False) as app, make_client(app) as http:
        account = await register(http)
        attempt = {"email": account.email, "password": "wrong password guess"}
        statuses = {
            (await http.post(f"{API}/auth/login", json=attempt)).status_code for _ in range(30)
        }
    assert statuses == {401}


# -------------------------------------------------------------- session lifecycle


async def test_me_requires_a_session(client: httpx.AsyncClient) -> None:
    anonymous = await client.get(f"{API}/auth/me")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "not_authenticated"

    account = await register_and_login(client)
    signed_in = await client.get(f"{API}/auth/me")
    assert signed_in.status_code == 200
    assert signed_in.json()["id"] == str(account.id)


async def test_a_forged_or_tampered_access_cookie_is_rejected(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    await register_and_login(client)
    genuine = client.cookies.get("w2a_access")
    assert genuine is not None
    header, payload, signature = genuine.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

    for forged in (f"{header}.{payload}.{flipped}", f"{header}.{payload}.", "garbage"):
        async with make_client() as attacker:
            attacker.cookies.set("w2a_access", forged, domain="testserver.local", path="/")
            response = await attacker.get(f"{API}/auth/me")
        assert response.status_code == 401


async def test_access_token_expires_and_refresh_restores_the_session(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    await register_and_login(client)
    clock.advance(minutes=14)
    assert (await client.get(f"{API}/auth/me")).status_code == 200

    clock.advance(minutes=2)  # 16 minutes: the access token is past its 15-minute life
    assert (await client.get(f"{API}/auth/me")).status_code == 401

    refreshed = await client.post(f"{API}/auth/refresh")
    assert refreshed.status_code == 200
    assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_refresh_rotates_the_token(client: httpx.AsyncClient) -> None:
    await register_and_login(client)
    before = client.cookies.get("w2a_refresh")
    response = await client.post(f"{API}/auth/refresh")
    after = client.cookies.get("w2a_refresh")

    assert response.status_code == 200
    assert before and after and before != after


async def test_refresh_requires_the_cookie(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{API}/auth/refresh")
    assert response.status_code == 401


async def test_refresh_token_expires_after_thirty_days(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    await register_and_login(client)
    clock.advance(days=30, seconds=1)
    assert (await client.post(f"{API}/auth/refresh")).status_code == 401


async def test_active_use_keeps_a_session_alive_beyond_thirty_days(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    await register_and_login(client)
    for _ in range(5):
        clock.advance(days=20)
        assert (await client.post(f"{API}/auth/refresh")).status_code == 200
    assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_replaying_a_rotated_refresh_token_revokes_the_whole_session(
    client: httpx.AsyncClient,
    make_client: ClientFactory,
    clock: MutableClock,
    session_factory: Sessions,
) -> None:
    """A copied refresh token used after the owner has rotated it: theft is assumed and
    both parties are signed out."""
    await register_and_login(client)
    async with make_client() as thief:
        copy_cookies(client, thief)  # the token is copied...
        assert (await client.post(f"{API}/auth/refresh")).status_code == 200  # ...then rotated
        clock.advance(minutes=1)

        replay = await thief.post(f"{API}/auth/refresh")

    assert replay.status_code == 401
    assert replay.json()["code"] == "session_revoked"
    # The legitimate browser is signed out as well: its newer token no longer works.
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    assert (await client.post(f"{API}/auth/refresh")).status_code == 401
    assert "auth.refresh_reuse_detected" in await audit_actions(session_factory)


async def test_simultaneous_refreshes_do_not_sign_the_user_out(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    """Two tabs refreshing at the same instant is not theft: one wins, none are revoked."""
    await register_and_login(client)

    async def refresh_from_another_tab() -> int:
        async with make_client() as tab:
            copy_cookies(client, tab)
            return (await tab.post(f"{API}/auth/refresh")).status_code

    statuses = await asyncio.gather(*(refresh_from_another_tab() for _ in range(6)))

    assert sorted(statuses) == [200] + [401] * 5
    # The session survived: the access token issued at login is still honoured.
    assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_logout_ends_the_session_on_the_server(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    await register_and_login(client)
    async with make_client() as replayer:
        copy_cookies(client, replayer)  # cookies captured before the logout

        response = await client.post(f"{API}/auth/logout")

        assert response.status_code == 204
        cleared = response.headers.get_list("set-cookie")
        assert len(cleared) == 2 and all("max-age=0" in c.lower() for c in cleared)
        assert (await client.get(f"{API}/auth/me")).status_code == 401
        # Not just forgotten by the browser: the old cookies are dead on the server too.
        assert (await replayer.get(f"{API}/auth/me")).status_code == 401
        assert (await replayer.post(f"{API}/auth/refresh")).status_code == 401
    assert (await audit_actions(session_factory))[-1] == "auth.logout"


async def test_logout_works_with_an_expired_access_token_and_without_a_session(
    client: httpx.AsyncClient, make_client: ClientFactory, clock: MutableClock
) -> None:
    assert (await client.post(f"{API}/auth/logout")).status_code == 204  # nothing to end

    await register_and_login(client)
    async with make_client() as replayer:
        copy_cookies(client, replayer)
        clock.advance(minutes=20)  # access token expired; only the refresh cookie identifies us
        assert (await client.post(f"{API}/auth/logout")).status_code == 204
        assert (await replayer.post(f"{API}/auth/refresh")).status_code == 401


async def test_sessions_are_independent_until_one_logs_out(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    account = await register_and_login(client)
    async with make_client() as phone_browser:
        await login(phone_browser, account)
        await client.post(f"{API}/auth/logout")
        assert (await phone_browser.get(f"{API}/auth/me")).status_code == 200


# ------------------------------------------------------------- account management


async def test_change_password_signs_out_other_sessions_only(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    account = await register_and_login(client)
    new_password = "a brand new long password"
    async with make_client() as other_browser:
        await login(other_browser, account)

        wrong = await client.post(
            f"{API}/auth/password",
            json={"current_password": "not my password", "new_password": new_password},
        )
        changed = await client.post(
            f"{API}/auth/password",
            json={"current_password": account.password, "new_password": new_password},
        )

        assert wrong.status_code == 401
        assert changed.status_code == 204
        assert (await client.get(f"{API}/auth/me")).status_code == 200  # this session stays
        assert (await other_browser.get(f"{API}/auth/me")).status_code == 401
        assert (await other_browser.post(f"{API}/auth/refresh")).status_code == 401

    async with make_client() as fresh:
        old = await fresh.post(
            f"{API}/auth/login", json={"email": account.email, "password": account.password}
        )
        new = await fresh.post(
            f"{API}/auth/login", json={"email": account.email, "password": new_password}
        )
    assert (old.status_code, new.status_code) == (401, 200)
    assert "account.password_change" in await audit_actions(session_factory)


async def test_new_password_must_meet_the_policy(client: httpx.AsyncClient) -> None:
    account = await register_and_login(client)
    response = await client.post(
        f"{API}/auth/password",
        json={"current_password": account.password, "new_password": "tooshort"},
    )
    assert response.status_code == 422


async def test_profile_update_validates_the_timezone(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await register_and_login(client)

    updated = await client.patch(
        f"{API}/auth/me", json={"display_name": "Sérgio", "timezone": "Atlantic/Azores"}
    )
    invalid = await client.patch(f"{API}/auth/me", json={"timezone": "Europe/Lisboa"})
    forbidden_field = await client.patch(f"{API}/auth/me", json={"role": "admin"})

    assert updated.status_code == 200
    assert (updated.json()["display_name"], updated.json()["timezone"]) == (
        "Sérgio",
        "Atlantic/Azores",
    )
    assert invalid.status_code == 422
    assert forbidden_field.status_code == 422
    me = (await client.get(f"{API}/auth/me")).json()
    assert (me["timezone"], me["role"]) == ("Atlantic/Azores", "admin")
    assert "account.update" in await audit_actions(session_factory)


async def test_a_disabled_account_cannot_sign_in_and_loses_its_sessions(
    client: httpx.AsyncClient, make_client: ClientFactory, session_factory: Sessions
) -> None:
    account = await register_and_login(client)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE users SET account_status = 'suspended'"))

    assert (await client.get(f"{API}/auth/me")).status_code == 401
    assert (await client.post(f"{API}/auth/refresh")).status_code == 401
    async with make_client() as fresh:
        response = await fresh.post(
            f"{API}/auth/login", json={"email": account.email, "password": account.password}
        )
    assert response.status_code == 403
    assert response.json()["code"] == "account_disabled"


async def test_refresh_token_is_stored_only_as_a_hash(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await register_and_login(client)
    token = client.cookies.get("w2a_refresh")
    assert token is not None
    async with session_factory() as session:
        stored = (await session.scalars(text("SELECT token_hash FROM auth_sessions"))).all()
        dump = str((await session.execute(text("SELECT * FROM auth_sessions"))).all())
    assert len(stored) == 1 and len(stored[0]) == 64
    assert token not in dump


async def test_session_lifetimes_follow_the_clock(
    client: httpx.AsyncClient, clock: MutableClock, session_factory: Sessions
) -> None:
    await register_and_login(client)
    async with session_factory() as session:
        created, expires = (
            await session.execute(text("SELECT created_at, expires_at FROM auth_sessions"))
        ).one()
    assert created == clock.now()
    assert expires - created == timedelta(days=30)
