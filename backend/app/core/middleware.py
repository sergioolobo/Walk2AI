"""Request context middleware (pure ASGI, so streaming responses are never buffered).

Per request it: assigns a correlation id, adds defensive response headers, writes one
structured access-log line, and turns any unhandled exception into a generic
problem+json 500 that leaks nothing but the request id.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import problem
from app.core.logging import set_request_id

logger = logging.getLogger("walk2ai.access")
error_logger = logging.getLogger("walk2ai.errors")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # API responses carry balances and personal data: never cache them.
    "Cache-Control": "no-store",
}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        set_request_id(request_id)

        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in _DEFAULT_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            error_logger.error("unhandled exception", exc_info=exc)
            if response_started:
                raise
            response = problem(
                status=500,
                code="internal_error",
                title="Internal error",
                detail="An unexpected error occurred. Quote the request id when reporting it.",
            )
            await response(scope, receive, send_with_headers)
        finally:
            client = scope.get("client")
            level = logging.DEBUG if scope["path"].endswith("/health") else logging.INFO
            logger.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],  # path only: query strings are never logged
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client": client[0] if client else None,
                },
            )
