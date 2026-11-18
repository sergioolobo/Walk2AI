"""Typed application errors and their mapping to RFC 9457 problem+json responses.

Services raise these; nothing below the API layer knows about HTTP beyond the status
code an error class carries. ``code`` is the stable, machine-readable identifier
clients switch on.
"""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import current_request_id

logger = logging.getLogger("walk2ai.errors")

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


class NotAuthenticated(AppError):
    status_code = 401
    code = "not_authenticated"
    title = "Authentication required"


class InvalidCredentials(NotAuthenticated):
    code = "invalid_credentials"
    title = "Invalid credentials"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    title = "Not allowed"


class CsrfRejected(Forbidden):
    code = "cross_origin_rejected"
    title = "Cross-origin request rejected"


class RegistrationClosed(Forbidden):
    code = "registration_closed"
    title = "Registration is closed"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    title = "Not found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class EmailTaken(Conflict):
    code = "email_taken"
    title = "E-mail already registered"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"
    title = "Validation failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


def problem(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "request_id": current_request_id(),
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


_HTTP_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("application error: %s", exc.detail, exc_info=exc)
        return problem(
            status=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            headers=exc.headers,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values back: a rejected payload may contain a password.
        errors = [
            {
                "field": ".".join(str(part) for part in err["loc"] if part != "body"),
                "message": err["msg"],
                "type": err["type"],
            }
            for err in exc.errors()
        ]
        return problem(
            status=422,
            code="validation_error",
            title="Validation failed",
            detail="The request is invalid.",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return problem(
            status=exc.status_code,
            code=_HTTP_CODES.get(exc.status_code, "http_error"),
            title=detail,
            detail=detail,
            headers=exc.headers,
        )

    # Unhandled exceptions are turned into a generic 500 by RequestContextMiddleware,
    # which wraps the whole application and so also covers failures outside routing.
