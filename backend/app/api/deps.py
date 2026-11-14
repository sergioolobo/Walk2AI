"""Shared FastAPI dependencies: infrastructure handles, authentication, authorisation."""

import uuid
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import get_session
from app.core.errors import CsrfRejected, Forbidden, NotAuthenticated
from app.core.ratelimit import client_ip
from app.core.security import PasswordService
from app.models.user import DeviceToken, User
from app.services.audit import RequestMeta
from app.services.auth import AuthService, DeviceService
from app.services.auth.devices import SCOPE_FITNESS_SYNC
from app.services.fitness.ingestion import FitnessIngestionService
from app.services.rewards.rules import RewardRuleService
from app.services.wallet import WalletService

ACCESS_COOKIE = "w2a_access"
REFRESH_COOKIE = "w2a_refresh"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_passwords(request: Request) -> PasswordService:
    passwords: PasswordService = request.app.state.passwords
    return passwords


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Clock, Depends(get_clock)]


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(ip_address=client_ip(request), user_agent=request.headers.get("user-agent"))


RequestMetaDep = Annotated[RequestMeta, Depends(get_request_meta)]


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    passwords: Annotated[PasswordService, Depends(get_passwords)],
) -> AuthService:
    return AuthService(session, settings=settings, clock=clock, passwords=passwords)


def get_device_service(
    session: SessionDep, settings: SettingsDep, clock: ClockDep
) -> DeviceService:
    return DeviceService(session, settings=settings, clock=clock)


def get_wallet_service(session: SessionDep, clock: ClockDep) -> WalletService:
    return WalletService(session, clock)


def get_ingestion_service(
    session: SessionDep, settings: SettingsDep, clock: ClockDep
) -> FitnessIngestionService:
    return FitnessIngestionService(session, settings=settings, clock=clock)


def get_reward_rule_service(session: SessionDep, clock: ClockDep) -> RewardRuleService:
    return RewardRuleService(session, clock)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
IngestionServiceDep = Annotated[FitnessIngestionService, Depends(get_ingestion_service)]
RewardRuleServiceDep = Annotated[RewardRuleService, Depends(get_reward_rule_service)]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]
WalletServiceDep = Annotated[WalletService, Depends(get_wallet_service)]


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def enforce_same_origin(request: Request, settings: SettingsDep) -> None:
    """Reject cross-site state-changing requests made with ambient (cookie) credentials.

    Layered on top of ``SameSite`` cookies. Requests authenticated by an explicit
    ``Authorization`` header (the mobile companion) are not exposed to CSRF and pass.
    """
    if request.method not in _UNSAFE_METHODS or "authorization" in request.headers:
        return
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise CsrfRejected("Cross-site requests are not accepted.")
    origin = request.headers.get("origin")
    if origin is not None and origin != _origin_of(settings.public_origin):
        raise CsrfRejected("Requests from this origin are not accepted.")


@dataclass(frozen=True)
class Principal:
    """The signed-in browser user."""

    user: User
    login_id: uuid.UUID


async def get_principal(request: Request, auth: AuthServiceDep) -> Principal:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise NotAuthenticated("Sign in to continue.")
    user, login_id = await auth.authenticate(token)
    return Principal(user=user, login_id=login_id)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def require_admin(principal: PrincipalDep) -> Principal:
    if not principal.user.is_admin:
        raise Forbidden("Administrator access is required.")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


@dataclass(frozen=True)
class DevicePrincipal:
    """A paired device acting for its user, limited to its token's scopes."""

    user: User
    device: DeviceToken


def _bearer_token(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise NotAuthenticated(
            "A device token is required.", headers={"WWW-Authenticate": "Bearer"}
        )
    return value.strip()


async def require_fitness_device(request: Request, devices: DeviceServiceDep) -> DevicePrincipal:
    user, device = await devices.authenticate(
        _bearer_token(request), required_scope=SCOPE_FITNESS_SYNC
    )
    return DevicePrincipal(user=user, device=device)


FitnessDeviceDep = Annotated[DevicePrincipal, Depends(require_fitness_device)]


async def get_fitness_reader(
    request: Request, auth: AuthServiceDep, devices: DeviceServiceDep
) -> User:
    """The user behind either credential: a paired device or a browser session.

    Used only by read-only fitness status routes that both the companion and the web
    app need.
    """
    if "authorization" in request.headers:
        user, _ = await devices.authenticate(
            _bearer_token(request), required_scope=SCOPE_FITNESS_SYNC
        )
        return user
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise NotAuthenticated("Sign in to continue.")
    user, _ = await auth.authenticate(token)
    return user


FitnessReaderDep = Annotated[User, Depends(get_fitness_reader)]
