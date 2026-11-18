"""Authentication and account endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    AuthServiceDep,
    ClockDep,
    PrincipalDep,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
)
from app.core.config import Settings
from app.core.errors import AppError, NotAuthenticated
from app.core.ratelimit import enforce_rate_limit, rate_limit
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserResponse,
)
from app.services.auth import IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])

# The refresh token is only ever sent to the auth endpoints, never to the rest of the API.
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_session_cookies(
    response: Response, issued: IssuedSession, settings: Settings, now: datetime
) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        issued.access_token,
        max_age=int((issued.access_expires_at - now).total_seconds()),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=int((issued.refresh_expires_at - now).total_seconds()),
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        ACCESS_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", limit=10, window_seconds=3600))],
)
async def register(
    body: RegisterRequest, session: SessionDep, auth: AuthServiceDep, meta: RequestMetaDep
) -> User:
    """Create an account. Subject to REGISTRATION_MODE; the first account is the admin."""
    user = await auth.register(
        email=body.email, password=body.password, display_name=body.display_name, meta=meta
    )
    await session.commit()
    return user


@router.post(
    "/login",
    response_model=UserResponse,
    dependencies=[Depends(rate_limit("login-ip", limit=20, window_seconds=60))],
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> User:
    """Verify credentials and set the session cookies."""
    enforce_rate_limit(
        request, "login-account", limit=10, window_seconds=300, subject=body.email.lower()
    )
    try:
        issued = await auth.login(email=body.email, password=body.password, meta=meta)
    except AppError:
        await session.commit()  # keep the audit record of the failed attempt
        raise
    await session.commit()
    _set_session_cookies(response, issued, settings, clock.now())
    return issued.user


@router.post(
    "/refresh",
    response_model=UserResponse,
    dependencies=[Depends(rate_limit("refresh", limit=60, window_seconds=60))],
)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> User:
    """Rotate the refresh token and issue a new access token."""
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise NotAuthenticated("Sign in to continue.")
    try:
        issued = await auth.refresh(token, meta)
    except AppError:
        await session.commit()  # a detected token reuse revokes the session: persist it
        raise
    await session.commit()
    _set_session_cookies(response, issued, settings, clock.now())
    return issued.user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session: SessionDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> Response:
    """End the login session and clear the cookies. Always succeeds."""
    login_id = None
    access_token = request.cookies.get(ACCESS_COOKIE)
    if access_token:
        try:
            _, login_id = await auth.authenticate(access_token)
        except NotAuthenticated:
            login_id = None
    await auth.logout(
        login_id=login_id, refresh_token=request.cookies.get(REFRESH_COOKIE), meta=meta
    )
    await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response, settings)
    return response


@router.get("/me", response_model=UserResponse)
async def me(principal: PrincipalDep) -> User:
    return principal.user


@router.patch("/me", response_model=UserResponse)
async def update_me(
    body: UpdateProfileRequest,
    principal: PrincipalDep,
    session: SessionDep,
    auth: AuthServiceDep,
    meta: RequestMetaDep,
) -> User:
    """Update display name and timezone. The timezone defines the user's "day"."""
    user = await auth.update_profile(
        principal.user, display_name=body.display_name, timezone=body.timezone, meta=meta
    )
    await session.commit()
    return user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    auth: AuthServiceDep,
    meta: RequestMetaDep,
) -> Response:
    """Change the password. Every other login session is signed out."""
    enforce_rate_limit(
        request, "password-change", limit=5, window_seconds=300, subject=str(principal.user.id)
    )
    await auth.change_password(
        principal.user,
        current_password=body.current_password,
        new_password=body.new_password,
        keep_login_id=principal.login_id,
        meta=meta,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
