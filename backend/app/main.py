"""Application factory.

Run with:  uvicorn app.main:create_app --factory
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI

from app.api.router import API_PREFIX, api_router
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db import build_engine, build_session_factory
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security import PasswordService
from app.services.ai.gateway import AIGateway, wait_for_inflight
from app.services.ai.providers import ProviderAdapter, build_providers
from app.services.maintenance import run_maintenance

logger = logging.getLogger("walk2ai")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
__version__ = "0.1.0"


def expected_schema_revision() -> str | None:
    """The migration head this build was written against."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    return ScriptDirectory.from_config(config).get_current_head()


# How long shutdown waits for AI requests in progress to finish and be charged.
SHUTDOWN_GRACE_SECONDS = 20


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    providers: Mapping[str, ProviderAdapter] | None = None,
) -> FastAPI:
    """``providers`` replaces the adapters built from the configured API keys (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    app_clock: Clock = clock or SystemClock()
    # A mutable mapping shared with the gateway: whatever is in it is what can be called.
    ai_providers: dict[str, ProviderAdapter] = (
        dict(providers) if providers is not None else build_providers(settings)
    )
    ai_inflight: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        sweeper = asyncio.create_task(
            run_maintenance(
                session_factory,
                app_clock,
                interval=timedelta(seconds=settings.reservation_sweep_interval_seconds),
            )
        )
        logger.info("walk2ai backend started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # Requests in progress are paid for: let them finish and settle first.
            await wait_for_inflight(ai_inflight, SHUTDOWN_GRACE_SECONDS)
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            for adapter in ai_providers.values():
                with contextlib.suppress(Exception):
                    await adapter.aclose()
            await engine.dispose()

    docs = settings.interactive_docs
    app = FastAPI(
        title="Walk2AI API",
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json" if docs else None,
        docs_url=f"{API_PREFIX}/docs" if docs else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = app_clock
    app.state.rate_limiter = SlidingWindowLimiter()
    app.state.ai_providers = ai_providers
    app.state.ai_inflight = ai_inflight
    app.state.ai_gateway = AIGateway(
        session_factory,
        settings=settings,
        clock=app_clock,
        providers=ai_providers,
        inflight=ai_inflight,
    )
    app.state.schema_head = expected_schema_revision()
    app.state.passwords = PasswordService(
        time_cost=settings.argon2_time_cost,
        memory_cost_kib=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    )

    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)
    return app
