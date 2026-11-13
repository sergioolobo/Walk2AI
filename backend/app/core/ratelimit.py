"""In-process sliding-window rate limiting.

Deliberately not Redis: Walk2AI runs as a single backend process for a single user
(see docs/DECISIONS.md, D-03). The reverse proxy adds a second, coarser limit at the
edge in production. If the backend is ever scaled to several processes, this module is
the one place to swap in a shared store.
"""

import math
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import Request

from app.core.errors import RateLimited

_MAX_TRACKED_KEYS = 20_000


class SlidingWindowLimiter:
    def __init__(self, time_source: Callable[[], float] = time.monotonic) -> None:
        self._time = time_source
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, *, limit: int, window_seconds: float) -> float | None:
        """Record an attempt. Returns None if allowed, else seconds until a slot frees."""
        now = self._time()
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= _MAX_TRACKED_KEYS:
                self._evict(now, window_seconds)
            hits = self._hits[key] = deque()
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return hits[0] + window_seconds - now
        hits.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()

    def _evict(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) >= _MAX_TRACKED_KEYS:
            # Under a key-flooding attack, fail closed on memory rather than grow.
            self._hits.clear()


def client_ip(request: Request) -> str:
    # Behind the reverse proxy uvicorn is started with --proxy-headers and a trusted
    # proxy address, so request.client already reflects the real client.
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(
    request: Request, bucket: str, *, limit: int, window_seconds: int, subject: str | None = None
) -> None:
    """Raise RateLimited when ``subject`` (default: the client IP) exceeds the limit."""
    if not request.app.state.settings.rate_limit_enabled:
        return
    limiter: SlidingWindowLimiter = request.app.state.rate_limiter
    key = f"{bucket}:{subject if subject is not None else client_ip(request)}"
    retry_after = limiter.hit(key, limit=limit, window_seconds=window_seconds)
    if retry_after is not None:
        raise RateLimited(
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )


def rate_limit(
    bucket: str, *, limit: int, window_seconds: int
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: per-client-IP limit for a route group."""

    async def dependency(request: Request) -> None:
        enforce_rate_limit(request, bucket, limit=limit, window_seconds=window_seconds)

    return dependency
