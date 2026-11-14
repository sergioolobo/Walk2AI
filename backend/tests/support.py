"""Helpers shared by the test-suite."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.models.enums import TransactionType
from app.models.user import User
from app.models.wallet import WalletAccount
from app.services.wallet import WalletService

DEFAULT_PASSWORD = "correct horse battery staple"
START_INSTANT = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

API = "/api/v1"


class MutableClock:
    """A clock the test controls. Time moves only when a test says so."""

    def __init__(self, start: datetime = START_INSTANT) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, instant: datetime) -> None:
        self._now = instant

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)

    def reset(self) -> None:
        self._now = START_INSTANT


@dataclass(frozen=True)
class Account:
    id: uuid.UUID
    email: str
    password: str
    role: str


async def register(
    client: httpx.AsyncClient, email: str = "owner@example.com", password: str = DEFAULT_PASSWORD
) -> Account:
    response = await client.post(
        f"{API}/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Account(id=uuid.UUID(body["id"]), email=email, password=password, role=body["role"])


async def login(client: httpx.AsyncClient, account: Account) -> httpx.Response:
    response = await client.post(
        f"{API}/auth/login", json={"email": account.email, "password": account.password}
    )
    assert response.status_code == 200, response.text
    return response


async def register_and_login(
    client: httpx.AsyncClient, email: str = "owner@example.com", password: str = DEFAULT_PASSWORD
) -> Account:
    account = await register(client, email, password)
    await login(client, account)
    return account


async def create_user_with_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    email: str | None = None,
    balance: int = 0,
) -> uuid.UUID:
    """Insert a user directly (no HTTP, no real password) and optionally fund the wallet."""
    async with session_factory() as session, session.begin():
        now = clock.now()
        user = User(
            email=email or f"user-{uuid.uuid4().hex[:12]}@example.com",
            password_hash="not-a-real-hash",
            timezone="Europe/Lisbon",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        wallets = WalletService(session, clock)
        await wallets.create_wallet(user.id)
        if balance:
            await wallets.adjust(
                user.id,
                balance,
                kind=TransactionType.ADMIN_ADJUSTMENT,
                reason="test funding",
                actor_id=None,
            )
        return user.id


async def wallet_state(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> WalletAccount:
    async with session_factory() as session:
        wallet = await session.scalar(select(WalletAccount).where(WalletAccount.user_id == user_id))
        assert wallet is not None
        return wallet


async def assert_ledger_consistent(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    """Every wallet's balances must equal what its ledger says. Used after money tests."""
    async with session_factory() as session:
        report = await WalletService(session, clock).reconcile()
    assert report.ok, f"ledger out of balance: {report.discrepancies}"


# --------------------------------------------------------------------- fitness


async def seed_configuration(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """The initial reward rules, workout types and EUR->WC rate."""
    from app.seeds import seed_initial_configuration

    async with session_factory() as session, session.begin():
        await seed_initial_configuration(session)


async def pair_device(client: httpx.AsyncClient, name: str = "iPhone 17 Pro") -> str:
    """Pair a device for the signed-in user of ``client``; returns the device token."""
    code = (await client.post(f"{API}/auth/devices/pairing-code")).json()["code"]
    # Pairing needs no session; an Authorization header keeps cookies out of the picture.
    response = await client.post(
        f"{API}/auth/devices/pair",
        json={"code": code, "name": name},
        headers={"Authorization": "Bearer pairing"},
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["token"]
    return token


def day(date_: str, steps: int, **extra: object) -> dict[str, Any]:
    return {"date": date_, "steps": steps, **extra}


def workout(
    external_id: str,
    kind: str,
    start: str,
    minutes: int,
    *,
    active_minutes: int | None = None,
    **extra: object,
) -> dict[str, Any]:
    """A workout payload. ``start`` is ISO 8601 with an offset."""
    begin = datetime.fromisoformat(start)
    active = minutes if active_minutes is None else active_minutes
    return {
        "external_id": external_id,
        "type": kind,
        "start": begin.isoformat(),
        "end": (begin + timedelta(minutes=minutes)).isoformat(),
        "duration_seconds": active * 60,
        **extra,
    }


async def sync(
    client: httpx.AsyncClient,
    token: str,
    *,
    days: list[dict[str, Any]] | None = None,
    workouts: list[dict[str, Any]] | None = None,
    deleted: list[str] | None = None,
    batch_id: str | None = None,
    expect: int = 200,
) -> dict[str, Any]:
    """POST /fitness/sync as a paired device and return the JSON body."""
    response = await client.post(
        f"{API}/fitness/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "batch_id": batch_id or str(uuid.uuid4()),
            "days": days or [],
            "workouts": workouts or [],
            "deleted_workout_ids": deleted or [],
        },
    )
    assert response.status_code == expect, response.text
    body: dict[str, Any] = response.json()
    return body
