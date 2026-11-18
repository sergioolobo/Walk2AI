"""Administration endpoints: role enforcement, audited adjustments, reconciliation."""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import AppFactory, ClientFactory
from tests.support import API, DEFAULT_PASSWORD, MutableClock, register_and_login

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


def adjustment(user_id: uuid.UUID | str, amount: int, **extra: object) -> dict[str, object]:
    return {"user_id": str(user_id), "amount_wc": amount, "reason": "test adjustment", **extra}


async def test_admin_routes_reject_anonymous_callers(client: httpx.AsyncClient) -> None:
    responses = [
        await client.post(f"{API}/admin/wallet/adjustments", json=adjustment(uuid.uuid4(), 100)),
        await client.get(f"{API}/admin/wallet/reconcile"),
        await client.get(f"{API}/admin/audit-log"),
    ]
    assert [r.status_code for r in responses] == [401, 401, 401]


async def test_admin_routes_reject_ordinary_users(
    make_app: AppFactory, make_client: ClientFactory
) -> None:
    """An ordinary account cannot grant itself credits or read the audit log."""
    async with (
        make_app(registration_mode="open") as app,
        make_client(app) as admin,
        make_client(app) as user,
    ):
        await register_and_login(admin, "admin@example.com")
        account = await register_and_login(user, "user@example.com")
        assert account.role == "user"

        responses = [
            await user.post(
                f"{API}/admin/wallet/adjustments", json=adjustment(account.id, 1_000_000)
            ),
            await user.get(f"{API}/admin/wallet/reconcile"),
            await user.get(f"{API}/admin/audit-log"),
        ]
        assert [r.status_code for r in responses] == [403, 403, 403]
        assert {r.json()["code"] for r in responses} == {"forbidden"}
        assert (await user.get(f"{API}/wallet")).json()["available_wc"] == 0


async def test_role_changes_take_effect_immediately(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    """The role is read from the database on every request, never from the token."""
    await register_and_login(client)
    assert (await client.get(f"{API}/admin/wallet/reconcile")).status_code == 200
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE users SET role = 'user'"))
    assert (await client.get(f"{API}/admin/wallet/reconcile")).status_code == 403


async def test_adjustment_credits_the_wallet_and_is_audited(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    account = await register_and_login(client)

    response = await client.post(
        f"{API}/admin/wallet/adjustments",
        json={"user_id": str(account.id), "amount_wc": 2000, "reason": "  opening balance  "},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "ADMIN_ADJUSTMENT"
    assert (body["amount_wc"], body["balance_after_wc"]) == (2000, 2000)
    assert body["description"] == "opening balance"
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 2000

    async with session_factory() as session:
        audit = (
            await session.execute(
                text(
                    "SELECT actor_type, actor_user_id, entity_id, details FROM audit_log "
                    "WHERE action = 'wallet.admin_adjustment'"
                )
            )
        ).one()
        created_by = await session.scalar(
            text(
                "SELECT created_by FROM wallet_transactions "
                "WHERE transaction_type = 'ADMIN_ADJUSTMENT'"
            )
        )
    assert audit.actor_type == "admin"
    assert audit.actor_user_id == account.id
    assert audit.entity_id == str(body["id"])
    assert audit.details == {
        "user_id": str(account.id),
        "amount_wc": 2000,
        "reason": "opening balance",
        "balance_after_wc": 2000,
    }
    assert created_by == account.id


async def test_negative_adjustment_cannot_overdraw(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    account = await register_and_login(client)
    await client.post(f"{API}/admin/wallet/adjustments", json=adjustment(account.id, 500))

    debit = await client.post(f"{API}/admin/wallet/adjustments", json=adjustment(account.id, -200))
    overdraw = await client.post(
        f"{API}/admin/wallet/adjustments", json=adjustment(account.id, -301)
    )

    assert debit.status_code == 201
    assert overdraw.status_code == 402
    assert overdraw.json()["code"] == "insufficient_credits"
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 300
    async with session_factory() as session:
        # The refused adjustment left neither a ledger row nor an audit entry.
        ledger_rows = await session.scalar(text("SELECT count(*) FROM wallet_transactions"))
        audited = await session.scalar(
            text("SELECT count(*) FROM audit_log WHERE action = 'wallet.admin_adjustment'")
        )
    assert (ledger_rows, audited) == (2, 2)


async def test_adjustment_with_idempotency_key_applies_once(client: httpx.AsyncClient) -> None:
    account = await register_and_login(client)
    payload = adjustment(account.id, 750, idempotency_key="opening-balance-2026")

    first = await client.post(f"{API}/admin/wallet/adjustments", json=payload)
    replay = await client.post(f"{API}/admin/wallet/adjustments", json=payload)
    conflicting = await client.post(
        f"{API}/admin/wallet/adjustments",
        json=adjustment(account.id, 900, idempotency_key="opening-balance-2026"),
    )

    assert (first.status_code, replay.status_code) == (201, 201)
    assert first.json()["id"] == replay.json()["id"]
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "idempotency_conflict"
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 750


@pytest.mark.parametrize(
    "payload",
    [
        {"amount_wc": 0, "reason": "zero"},
        {"amount_wc": 1.5, "reason": "fractional credits"},
        {"amount_wc": "lots", "reason": "not a number"},
        {"amount_wc": 100},  # no reason
        {"amount_wc": 100, "reason": "x"},  # reason too short to mean anything
        {"amount_wc": 100, "reason": "valid reason", "transaction_type": "ACTIVITY_REWARD"},
    ],
)
async def test_invalid_adjustments_are_rejected(
    client: httpx.AsyncClient, payload: dict[str, object]
) -> None:
    account = await register_and_login(client)
    response = await client.post(
        f"{API}/admin/wallet/adjustments", json={"user_id": str(account.id), **payload}
    )
    assert response.status_code == 422
    assert (await client.get(f"{API}/wallet")).json()["available_wc"] == 0


async def test_adjusting_an_unknown_user_is_a_404(client: httpx.AsyncClient) -> None:
    await register_and_login(client)
    response = await client.post(
        f"{API}/admin/wallet/adjustments", json=adjustment(uuid.uuid4(), 100)
    )
    assert response.status_code == 404


async def test_reconcile_reports_ok_and_then_detects_tampering(
    client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    account = await register_and_login(client)
    await client.post(f"{API}/admin/wallet/adjustments", json=adjustment(account.id, 500))

    clean = (await client.get(f"{API}/admin/wallet/reconcile")).json()
    assert clean == {"ok": True, "wallets_checked": 1, "discrepancies": []}

    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE wallet_accounts SET available_balance = 5000"))

    tampered = (await client.get(f"{API}/admin/wallet/reconcile")).json()
    assert tampered["ok"] is False
    assert tampered["discrepancies"] == [
        {
            "wallet_id": tampered["discrepancies"][0]["wallet_id"],
            "user_id": str(account.id),
            "check": "available_balance = sum(amount)",
            "expected": 500,
            "actual": 5000,
        }
    ]


async def test_audit_log_is_readable_paginated_and_holds_no_secrets(
    client: httpx.AsyncClient, make_client: ClientFactory
) -> None:
    account = await register_and_login(client)
    code = (await client.post(f"{API}/auth/devices/pairing-code")).json()["code"]
    async with make_client() as phone:
        device = (
            await phone.post(f"{API}/auth/devices/pair", json={"code": code, "name": "iPhone"})
        ).json()
    await client.post(
        f"{API}/auth/login", json={"email": account.email, "password": "a wrong password guess"}
    )
    await client.post(f"{API}/admin/wallet/adjustments", json=adjustment(account.id, 100))

    response = await client.get(f"{API}/admin/audit-log")
    entries = response.json()["items"]

    assert [e["action"] for e in entries] == [
        "wallet.admin_adjustment",
        "auth.login_failed",
        "device.paired",
        "device.pairing_code_created",
        "auth.login",
        "auth.register",
    ]
    assert entries[1]["details"] == {"reason": "wrong_password"}
    assert entries[1]["actor_type"] == "anonymous"
    assert all(e["ip_address"] == "127.0.0.1" for e in entries)

    # Nothing sensitive is ever written to the audit trail.
    for secret in (
        DEFAULT_PASSWORD,
        "a wrong password guess",
        device["token"],
        code,
        code.replace("-", ""),
        client.cookies.get("w2a_access") or "\x00",
        client.cookies.get("w2a_refresh") or "\x00",
    ):
        assert secret not in response.text

    page = (await client.get(f"{API}/admin/audit-log", params={"limit": 2})).json()
    assert [e["action"] for e in page["items"]] == ["wallet.admin_adjustment", "auth.login_failed"]
    following = (
        await client.get(
            f"{API}/admin/audit-log", params={"limit": 2, "cursor": page["next_cursor"]}
        )
    ).json()
    assert [e["action"] for e in following["items"]] == [
        "device.paired",
        "device.pairing_code_created",
    ]
    logins = (await client.get(f"{API}/admin/audit-log", params={"action": "auth.login"})).json()
    assert [e["action"] for e in logins["items"]] == ["auth.login"]
    assert logins["next_cursor"] is None


async def test_timestamps_in_the_ledger_follow_the_clock(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    account = await register_and_login(client)
    clock.advance(minutes=7)
    response = await client.post(
        f"{API}/admin/wallet/adjustments", json=adjustment(account.id, 100)
    )
    assert response.json()["created_at"].startswith("2026-11-13T12:07:00")
