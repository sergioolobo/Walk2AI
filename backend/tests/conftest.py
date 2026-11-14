"""Test fixtures.

Tests run against a real PostgreSQL database built by the real Alembic migrations —
row locks, CHECK and EXCLUDE constraints and triggers are part of what is under test,
so SQLite or ``metadata.create_all()`` would prove nothing.

The database is chosen with TEST_DATABASE_URL and its name must end in ``_test``: the
suite wipes it, and refuses to run against anything that is not obviously disposable.

Async test modules declare ``pytestmark = pytest.mark.anyio``.
"""

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import BACKEND_ROOT, create_app
from app.models import Base
from tests.support import MutableClock

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://walk2ai:walk2ai_dev@localhost:5432/walk2ai_test"

# Tables whose append-only triggers must be lifted while the suite resets state.
_APPEND_ONLY_TABLES = ("wallet_transactions", "audit_log")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_url() -> sa.URL:
    url = sa.make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))
    if not (url.database or "").endswith("_test"):
        raise RuntimeError(
            f"Refusing to run tests against database {url.database!r}: "
            "TEST_DATABASE_URL must name a database ending in '_test'."
        )
    return url


def alembic_config(url: sa.URL) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["sqlalchemy_url"] = url
    config.attributes["configure_logging"] = False
    return config


@pytest.fixture(scope="session")
def sync_engine(database_url: sa.URL) -> Iterator[sa.Engine]:
    engine = sa.create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_database(database_url: sa.URL, sync_engine: sa.Engine) -> None:
    """A clean database at the migration head, built once per test session."""
    with sync_engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(database_url), "head")


@pytest.fixture(scope="session")
def settings(database_url: sa.URL) -> Settings:
    return Settings(
        _env_file=None,  # never read a developer's .env in tests
        environment="test",
        database_url=SecretStr(database_url.render_as_string(hide_password=False)),
        jwt_secret=SecretStr("test-secret-" + "0123456789abcdef" * 3),
        public_origin="http://testserver",
        cookie_secure=False,
        registration_mode="first_user_only",
        # Cheapest valid Argon2 parameters: the suite hashes hundreds of passwords.
        argon2_time_cost=1,
        argon2_memory_cost_kib=8,
        argon2_parallelism=1,
    )


@pytest.fixture(scope="session")
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture(scope="session")
async def app(
    settings: Settings, clock: MutableClock, migrated_database: None
) -> AsyncIterator[FastAPI]:
    application = create_app(settings, clock=clock)
    yield application
    await application.state.engine.dispose()


@pytest.fixture
def db(migrated_database: None, sync_engine: sa.Engine, clock: MutableClock) -> None:
    """Empty every table and reset the clock before a test.

    Synchronous on purpose, so that plain (non-async) tests can use it too.
    """
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with sync_engine.begin() as connection:
        for table in _APPEND_ONLY_TABLES:
            connection.execute(sa.text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        connection.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        for table in _APPEND_ONLY_TABLES:
            connection.execute(sa.text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    clock.reset()


@pytest.fixture
def fresh_app(app: FastAPI, db: None) -> FastAPI:
    """The shared application over an empty database, with rate limits cleared."""
    app.state.rate_limiter.reset()
    return app


@pytest.fixture
def session_factory(fresh_app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = fresh_app.state.session_factory
    return factory


ClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def make_client(fresh_app: FastAPI) -> ClientFactory:
    """Open independent HTTP clients (separate cookie jars) against an application."""
    app = fresh_app

    @asynccontextmanager
    async def factory(
        application: FastAPI | None = None, *, base_url: str = "http://testserver"
    ) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=application or app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
            yield http

    return factory


@pytest.fixture
async def client(make_client: ClientFactory) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as http:
        yield http


AppFactory = Callable[..., AbstractAsyncContextManager[FastAPI]]


@pytest.fixture
def make_app(settings: Settings, clock: MutableClock, db: None) -> AppFactory:
    """Build an application with some settings changed (it shares the test database)."""

    @asynccontextmanager
    async def factory(**overrides: object) -> AsyncIterator[FastAPI]:
        application = create_app(settings.model_copy(update=overrides), clock=clock)
        try:
            yield application
        finally:
            await application.state.engine.dispose()

    return factory
