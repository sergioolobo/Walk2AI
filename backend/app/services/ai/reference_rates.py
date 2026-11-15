"""The USD→EUR conversion rate, kept up to date from the ECB's euro reference rate.

The European Central Bank publishes one reference rate per working day (the 14:15 CET
fixing, released around 16:00 CET), free and without an account. It is quoted as US
dollars per euro; provider costs are in dollars, so the rate stored is its inverse.

Rules:

* A new rate is a new *version* (from now on). Requests already charged keep the rate
  they were charged at.
* If the ECB cannot be reached, or publishes something implausible, nothing changes: the
  rate in force stays in force and the failure is reported.
* A rate set by hand holds until the ECB publishes a rate for that day or later.
* Nothing here ever runs inside a request: a slow or broken ECB cannot delay a chat.
"""

import asyncio
import csv
import io
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.models.enums import ActorType
from app.services.ai import catalogue
from app.services.ai.catalogue import CatalogueAdmin, CatalogueConflict
from app.services.audit import RequestMeta, record_audit

logger = logging.getLogger("walk2ai.rates")

BASE_CURRENCY = "USD"
QUOTE_CURRENCY = "EUR"

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
ECB_PARAMS = {"lastNObservations": "1", "format": "csvdata"}
ECB_SOURCE_PREFIX = "ECB reference rate "

_RATE_QUANTUM = Decimal("0.00000001")
# Outside these bounds a published figure is treated as an error, not as a rate. (The
# euro has traded between about 0.82 and 1.60 dollars since it was created.)
MIN_USD_PER_EUR = Decimal("0.5")
MAX_USD_PER_EUR = Decimal("2.5")
# The largest move from the previous ECB rate that is applied without a person looking.
# The euro's largest one-day move against the dollar is under 5%.
MAX_RELATIVE_CHANGE = Decimal("0.10")


class ReferenceRateUnavailable(Exception):
    """The reference rate could not be obtained or could not be trusted."""


@dataclass(frozen=True)
class ReferenceRate:
    published_for: date
    usd_per_eur: Decimal

    @property
    def usd_to_eur(self) -> Decimal:
        """EUR per dollar, rounded up: a converted cost is never understated."""
        return (Decimal(1) / self.usd_per_eur).quantize(_RATE_QUANTUM, rounding=ROUND_CEILING)

    @property
    def source(self) -> str:
        return (
            f"{ECB_SOURCE_PREFIX}{self.published_for.isoformat()}: "
            f"1 EUR = {format(self.usd_per_eur, 'f')} USD"
        )


RateFetcher = Callable[[], Awaitable[ReferenceRate]]


def parse_ecb_csv(text: str) -> ReferenceRate:
    """Read the latest observation from the ECB data API's CSV answer."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ReferenceRateUnavailable("The ECB answered without any observation.")
    row = rows[-1]
    if (row.get("CURRENCY"), row.get("CURRENCY_DENOM")) != (BASE_CURRENCY, QUOTE_CURRENCY):
        raise ReferenceRateUnavailable("The ECB answered with a different currency pair.")
    try:
        published_for = date.fromisoformat(row.get("TIME_PERIOD") or "")
        usd_per_eur = Decimal(row.get("OBS_VALUE") or "")
    except (ValueError, InvalidOperation) as error:
        raise ReferenceRateUnavailable("The ECB's answer could not be read.") from error
    if not usd_per_eur.is_finite() or not MIN_USD_PER_EUR <= usd_per_eur <= MAX_USD_PER_EUR:
        raise ReferenceRateUnavailable(
            f"The ECB published an implausible rate ({row.get('OBS_VALUE')}); it was ignored."
        )
    return ReferenceRate(published_for=published_for, usd_per_eur=usd_per_eur)


class EcbRateFetcher:
    """Fetches the latest USD/EUR reference rate from the ECB's public data API."""

    def __init__(
        self, *, http_client: httpx2.AsyncClient | None = None, timeout_seconds: int = 15
    ) -> None:
        self._client = http_client
        self._timeout = timeout_seconds

    async def __call__(self) -> ReferenceRate:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.get(
                ECB_URL, params=ECB_PARAMS, headers={"Accept": "text/csv"}
            )
            response.raise_for_status()
        except httpx2.HTTPError as error:
            raise ReferenceRateUnavailable(
                f"The ECB could not be reached ({type(error).__name__})."
            ) from error
        return parse_ecb_csv(response.text)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


# ------------------------------------------------------------------ refreshing


class Outcome(StrEnum):
    UPDATED = "updated"  # a new rate version is in force
    UNCHANGED = "unchanged"  # the ECB has not published anything newer
    KEPT_MANUAL = "kept_manual"  # a rate set by hand is newer than the ECB's
    REJECTED = "rejected"  # the published rate moved too far to apply unattended
    UNAVAILABLE = "unavailable"  # the ECB could not be reached or read


@dataclass(frozen=True)
class RefreshResult:
    outcome: Outcome
    detail: str
    at: datetime
    reference: ReferenceRate | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.UPDATED, Outcome.UNCHANGED, Outcome.KEPT_MANUAL)


@dataclass
class RefreshState:
    """What the last refresh did. Kept in memory: it describes this process only."""

    last: RefreshResult | None = None


def _is_ecb(source: str) -> bool:
    return source.startswith(ECB_SOURCE_PREFIX)


async def refresh_usd_eur_rate(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    fetch: RateFetcher,
    *,
    state: RefreshState | None = None,
    actor_user_id: uuid.UUID | None = None,
    meta: RequestMeta | None = None,
) -> RefreshResult:
    """Fetch the reference rate and, if it is news, make it the rate in force.

    Never raises for a failure to fetch: the result says what happened.
    """
    result = await _refresh(session_factory, clock, fetch, actor_user_id, meta)
    if state is not None:
        state.last = result
    log = logger.info if result.ok else logger.warning
    log("usd/eur rate refresh: %s", result.outcome.value, extra={"detail": result.detail})
    return result


async def _refresh(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    fetch: RateFetcher,
    actor_user_id: uuid.UUID | None,
    meta: RequestMeta | None,
) -> RefreshResult:
    now = clock.now()
    try:
        reference = await fetch()
    except ReferenceRateUnavailable as error:
        return RefreshResult(Outcome.UNAVAILABLE, f"{error} The rate in force is unchanged.", now)

    new_rate = reference.usd_to_eur
    async with session_factory() as session, session.begin():
        current = await catalogue.rate_in_force(session, BASE_CURRENCY, QUOTE_CURRENCY, now)
        if current is not None:
            if current.source == reference.source:
                return RefreshResult(
                    Outcome.UNCHANGED,
                    f"The ECB's latest rate ({reference.published_for.isoformat()}) "
                    "is already in force.",
                    now,
                    reference,
                )
            if not _is_ecb(current.source):
                if reference.published_for < current.effective_from.date():
                    return RefreshResult(
                        Outcome.KEPT_MANUAL,
                        "A rate set by hand is in force. It will be replaced when the ECB "
                        "publishes a rate for that day or later.",
                        now,
                        reference,
                    )
            elif abs(new_rate - current.rate) > current.rate * MAX_RELATIVE_CHANGE:
                return RefreshResult(
                    Outcome.REJECTED,
                    f"The ECB's rate for {reference.published_for.isoformat()} "
                    f"({format(new_rate, 'f')} EUR per USD) differs from the rate in force "
                    f"({format(current.rate, 'f')}) by more than 10%. It was not applied; "
                    "set the rate by hand if it is right.",
                    now,
                    reference,
                )
        try:
            row = await CatalogueAdmin(session, clock).create_rate(
                BASE_CURRENCY,
                QUOTE_CURRENCY,
                new_rate,
                effective_from=None,
                source=reference.source,
                created_by=actor_user_id,
            )
        except CatalogueConflict:
            return RefreshResult(
                Outcome.KEPT_MANUAL,
                "A rate scheduled by hand for a later moment exists; nothing was changed.",
                now,
                reference,
            )
        record_audit(
            session,
            clock,
            action="exchange_rate.auto_update",
            actor_type=ActorType.ADMIN if actor_user_id else ActorType.SYSTEM,
            actor_user_id=actor_user_id,
            entity_type="exchange_rate",
            entity_id=row.id,
            details={
                "base": BASE_CURRENCY,
                "quote": QUOTE_CURRENCY,
                "rate": format(new_rate, "f"),
                "source": reference.source,
                "previous_rate": format(current.rate, "f") if current else None,
            },
            meta=meta,
        )
    return RefreshResult(
        Outcome.UPDATED,
        f"1 USD = {format(new_rate, 'f')} EUR from now on ({reference.source}).",
        now,
        reference,
    )


async def run_rate_refresher(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    fetch: RateFetcher,
    *,
    interval: timedelta,
    state: RefreshState,
) -> None:
    """Refresh at start-up and then every ``interval``. Cancelled on shutdown."""
    while True:
        try:
            await refresh_usd_eur_rate(session_factory, clock, fetch, state=state)
        except Exception:
            logger.exception("usd/eur rate refresh failed")
        await asyncio.sleep(interval.total_seconds())
