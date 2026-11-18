"""Device pairing and device-token authentication.

A device (the iOS companion) never sees the account password or a browser session. The
signed-in user asks the web app for a one-time pairing code; the device exchanges it
for a long-lived token that is scoped to ``fitness:sync``. That token cannot read the
wallet or conversations, cannot spend credits and cannot reach admin routes.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import Forbidden, NotAuthenticated, NotFound
from app.core.security import (
    DEVICE_TOKEN_PREFIX,
    format_pairing_code,
    generate_pairing_code,
    generate_token,
    hash_pairing_code,
    hash_token,
)
from app.models.enums import ActorType
from app.models.user import DevicePairingCode, DeviceToken, User
from app.services.audit import RequestMeta, record_audit

SCOPE_FITNESS_SYNC = "fitness:sync"
DEFAULT_DEVICE_SCOPES = (SCOPE_FITNESS_SYNC,)


class InvalidPairingCode(NotAuthenticated):
    code = "invalid_pairing_code"
    title = "Pairing code is invalid or has expired"


class InsufficientScope(Forbidden):
    code = "insufficient_scope"
    title = "This device token is not allowed to do that"


@dataclass(frozen=True)
class PairingCode:
    code: str  # formatted for display, e.g. "ABCD-EFGH"
    expires_at: datetime


@dataclass(frozen=True)
class PairedDevice:
    device: DeviceToken
    token: str  # shown to the device exactly once; only its hash is stored


class DeviceService:
    def __init__(self, session: AsyncSession, *, settings: Settings, clock: Clock) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock

    async def create_pairing_code(self, user: User, meta: RequestMeta) -> PairingCode:
        now = self._clock.now()
        # One live code per user: asking for a new one invalidates any unused older one.
        await self._session.execute(
            delete(DevicePairingCode).where(
                DevicePairingCode.user_id == user.id, DevicePairingCode.used_at.is_(None)
            )
        )
        code = generate_pairing_code()
        expires_at = now + timedelta(minutes=self._settings.pairing_code_ttl_minutes)
        self._session.add(
            DevicePairingCode(
                user_id=user.id,
                code_hash=self._hash(code),
                created_at=now,
                expires_at=expires_at,
            )
        )
        record_audit(
            self._session,
            self._clock,
            action="device.pairing_code_created",
            actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
            actor_user_id=user.id,
            meta=meta,
        )
        await self._session.flush()
        return PairingCode(code=format_pairing_code(code), expires_at=expires_at)

    async def pair(self, *, code: str, name: str, platform: str, meta: RequestMeta) -> PairedDevice:
        now = self._clock.now()
        pairing = await self._session.scalar(
            select(DevicePairingCode)
            .where(DevicePairingCode.code_hash == self._hash(code))
            .with_for_update()
        )
        if pairing is None or pairing.used_at is not None or pairing.expires_at <= now:
            raise InvalidPairingCode("Pairing code is invalid or has expired.")
        user = await self._session.get(User, pairing.user_id)
        if user is None or not user.is_active:
            raise InvalidPairingCode("Pairing code is invalid or has expired.")

        token = generate_token(DEVICE_TOKEN_PREFIX)
        device = DeviceToken(
            user_id=user.id,
            name=name,
            platform=platform,
            token_hash=hash_token(token),
            scopes=list(DEFAULT_DEVICE_SCOPES),
            created_at=now,
        )
        self._session.add(device)
        await self._session.flush()
        pairing.used_at = now
        pairing.device_token_id = device.id
        record_audit(
            self._session,
            self._clock,
            action="device.paired",
            actor_type=ActorType.DEVICE,
            actor_user_id=user.id,
            entity_type="device",
            entity_id=device.id,
            details={"name": name, "platform": platform},
            meta=meta,
        )
        await self._session.flush()
        return PairedDevice(device=device, token=token)

    async def list_devices(self, user: User) -> Sequence[DeviceToken]:
        result = await self._session.scalars(
            select(DeviceToken)
            .where(DeviceToken.user_id == user.id)
            .order_by(DeviceToken.revoked_at.is_not(None), DeviceToken.created_at.desc())
        )
        return result.all()

    async def revoke(self, user: User, device_id: uuid.UUID, meta: RequestMeta) -> None:
        device = await self._session.scalar(
            select(DeviceToken)
            .where(DeviceToken.id == device_id, DeviceToken.user_id == user.id)
            .with_for_update()
        )
        if device is None:
            raise NotFound("Device not found.")
        if device.revoked_at is None:
            device.revoked_at = self._clock.now()
            record_audit(
                self._session,
                self._clock,
                action="device.revoked",
                actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
                actor_user_id=user.id,
                entity_type="device",
                entity_id=device.id,
                meta=meta,
            )
            await self._session.flush()

    async def authenticate(self, token: str, *, required_scope: str) -> tuple[User, DeviceToken]:
        """Resolve a device token or raise. Records when the device was last seen."""
        if not token.startswith(DEVICE_TOKEN_PREFIX):
            raise NotAuthenticated("Invalid device token.")
        device = await self._session.scalar(
            select(DeviceToken).where(DeviceToken.token_hash == hash_token(token))
        )
        if device is None or device.revoked_at is not None:
            raise NotAuthenticated("Invalid device token.")
        user = await self._session.get(User, device.user_id)
        if user is None or not user.is_active:
            raise NotAuthenticated("Invalid device token.")
        if required_scope not in device.scopes:
            raise InsufficientScope()
        device.last_used_at = self._clock.now()
        return user, device

    def _hash(self, code: str) -> str:
        return hash_pairing_code(code, secret=self._settings.jwt_secret.get_secret_value())
