"""Operational pieces: the command-line tools, the reservation sweeper, app lifespan."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.core.config import Settings, get_settings
from app.models.enums import ReservationStatus
from app.services.wallet import WalletService
from app.services.wallet.maintenance import run_reservation_sweeper
from tests.conftest import AppFactory
from tests.support import MutableClock, create_user_with_wallet, wallet_state

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def cli_environment(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, database_url: sa.URL, db: None
) -> Iterator[None]:
    """Point the CLI (which reads real environment variables) at the test database."""
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(hide_password=False))
    monkeypatch.setenv("JWT_SECRET", settings.jwt_secret.get_secret_value())
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_seed_is_idempotent(
    cli_environment: None, sync_engine: sa.Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["seed"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["seed"]) == 0
    second = capsys.readouterr().out

    assert "3 reward rule(s)" in first and "1 conversion rate(s) created" in first
    assert "0 reward rule(s), 0 workout type(s), 0 conversion rate(s) created" in second
    with sync_engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM reward_rules")).scalar() == 3


def test_cli_reconcile_exit_code_reflects_ledger_health(
    cli_environment: None, sync_engine: sa.Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    with sync_engine.begin() as connection:
        user_id = connection.execute(
            sa.text(
                "INSERT INTO users (email, password_hash) VALUES ('cli@example.com', 'x') "
                "RETURNING id"
            )
        ).scalar()
        connection.execute(
            sa.text("INSERT INTO wallet_accounts (user_id) VALUES (:u)"), {"u": user_id}
        )

    assert cli.main(["reconcile"]) == 0
    assert "1 wallet(s) checked, ok" in capsys.readouterr().out

    with sync_engine.begin() as connection:
        connection.execute(sa.text("UPDATE wallet_accounts SET available_balance = 42"))

    assert cli.main(["reconcile"]) == 1  # non-zero: usable in monitoring and CI
    output = capsys.readouterr().out
    assert "MISMATCH" in output and "expected 0, found 42" in output


def test_cli_rejects_unknown_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["drop-everything"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.anyio
async def test_sweeper_releases_expired_holds_and_survives_errors(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(
            user, 400, request_id=request_id, ttl=timedelta(minutes=1)
        )
    clock.advance(minutes=5)

    calls = 0

    class FlakyFactory:
        """Fails on its first use: one bad sweep must not stop the next."""

        def __call__(self) -> AsyncSession:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database briefly unavailable")
            return session_factory()

    sweeper = asyncio.create_task(
        run_reservation_sweeper(FlakyFactory(), clock, interval=timedelta(milliseconds=10))  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(10):
            # Polling database state: there is no in-process event to wait on.
            while (await wallet_state(session_factory, user)).reserved_balance != 0:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
    finally:
        sweeper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweeper

    assert calls >= 2
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1000, 0)
    async with session_factory() as session:
        reservation = await WalletService(session, clock).get_reservation(request_id)
    assert reservation.status == ReservationStatus.EXPIRED.value


@pytest.mark.anyio
async def test_application_lifespan_starts_and_stops_the_sweeper(make_app: AppFactory) -> None:
    async with make_app() as app:
        before = {task for task in asyncio.all_tasks() if not task.done()}
        async with app.router.lifespan_context(app):
            running = {task for task in asyncio.all_tasks() if not task.done()} - before
            assert len(running) == 1  # the reservation sweeper
        await asyncio.sleep(0)
        assert all(task.done() for task in running)
