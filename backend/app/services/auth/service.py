"""Registration, login, session rotation and account management.

Session model
  * A login creates a *login session*: a family of refresh tokens sharing a
    ``family_id``. Each refresh rotates the token (new row, old row revoked).
  * The access token is a short-lived signed JWT naming the user and the family. It is
    accepted only while the family still has a live token, so logout, password change
    and theft detection take effect immediately — not when the JWT happens to expire.
  * Presenting a refresh token that was already rotated means it was copied. The whole
    family is revoked. A short grace window absorbs the benign case of two browser tabs
    refreshing at the same instant.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import anyio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import (
    EmailTaken,
    Forbidden,
    InvalidCredentials,
    NotAuthenticated,
    RegistrationClosed,
    ValidationFailed,
)
from app.core.security import (
    InvalidAccessToken,
    PasswordService,
    create_access_token,
    decode_access_token,
    generate_token,
    hash_token,
)
from app.core.timeutil import is_valid_timezone
from app.models.enums import AccountStatus, ActorType, UserRole
from app.models.user import AuthSession, User
from app.services.audit import RequestMeta, record_audit
from app.services.wallet import WalletService

# Serialises registrations so that "the first user becomes the administrator" cannot be
# won twice. Arbitrary constant; scoped to this database.
_REGISTRATION_LOCK_KEY = 0x57324149_0001

_ROTATION_GRACE = timedelta(seconds=10)

REVOKED_ROTATED = "rotated"
REVOKED_LOGOUT = "logout"
REVOKED_REUSE = "reuse_detected"
REVOKED_PASSWORD_CHANGE = "password_change"  # noqa: S105 - a reason label, not a secret


class AccountDisabled(Forbidden):
    code = "account_disabled"
    title = "Account is disabled"


class RefreshTokenReused(NotAuthenticated):
    code = "session_revoked"
    title = "Session revoked"


@dataclass(frozen=True)
class IssuedSession:
    user: User
    login_id: uuid.UUID
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class AuthService:
    def __init__(
        self, session: AsyncSession, *, settings: Settings, clock: Clock, passwords: PasswordService
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._passwords = passwords

    # ------------------------------------------------------------ registration

    async def register(
        self, *, email: str, password: str, display_name: str | None, meta: RequestMeta
    ) -> User:
        mode = self._settings.registration_mode
        # Cheap refusal first, so a closed instance never spends CPU on hashing.
        if mode == "closed" or (mode == "first_user_only" and await self._user_count() > 0):
            raise RegistrationClosed("This instance does not accept new registrations.")

        password_hash = await anyio.to_thread.run_sync(self._passwords.hash, password)

        await self._session.execute(select(func.pg_advisory_xact_lock(_REGISTRATION_LOCK_KEY)))
        existing_users = await self._user_count()
        if mode == "first_user_only" and existing_users > 0:
            raise RegistrationClosed("This instance does not accept new registrations.")
        if await self._session.scalar(select(User.id).where(User.email == email)) is not None:
            raise EmailTaken("An account with this e-mail already exists.")

        now = self._clock.now()
        user = User(
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            role=(UserRole.ADMIN if existing_users == 0 else UserRole.USER).value,
            account_status=AccountStatus.ACTIVE.value,
            timezone=self._settings.default_timezone,
            created_at=now,
            updated_at=now,
        )
        self._session.add(user)
        await self._session.flush()
        await WalletService(self._session, self._clock).create_wallet(user.id)
        record_audit(
            self._session,
            self._clock,
            action="auth.register",
            actor_type=ActorType.USER,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"role": user.role},
            meta=meta,
        )
        return user

    # ------------------------------------------------------------------- login

    async def login(self, *, email: str, password: str, meta: RequestMeta) -> IssuedSession:
        """Verify credentials and open a login session.

        On failure an audit row is added before raising: the caller commits it.
        """
        user = await self._session.scalar(select(User).where(User.email == email))
        if user is None:
            # Same work as a real verification, so timing does not reveal the account.
            await anyio.to_thread.run_sync(self._passwords.verify_decoy, password)
            self._audit_login_failure(None, "unknown_account", meta)
            raise InvalidCredentials("E-mail or password is incorrect.")

        valid = await anyio.to_thread.run_sync(self._passwords.verify, user.password_hash, password)
        if not valid:
            self._audit_login_failure(user, "wrong_password", meta)
            raise InvalidCredentials("E-mail or password is incorrect.")
        if not user.is_active:
            self._audit_login_failure(user, "account_disabled", meta)
            raise AccountDisabled("This account is disabled.")

        if self._passwords.needs_rehash(user.password_hash):
            user.password_hash = await anyio.to_thread.run_sync(self._passwords.hash, password)

        issued = await self._issue(user, family_id=uuid.uuid4(), rotated_from=None, meta=meta)
        record_audit(
            self._session,
            self._clock,
            action="auth.login",
            actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
            actor_user_id=user.id,
            entity_type="login_session",
            entity_id=issued.login_id,
            meta=meta,
        )
        return issued

    async def refresh(self, refresh_token: str, meta: RequestMeta) -> IssuedSession:
        """Rotate a refresh token. May add rows before raising: the caller commits."""
        now = self._clock.now()
        row = await self._session.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == hash_token(refresh_token))
            .with_for_update()
        )
        if row is None:
            raise NotAuthenticated("Session is not valid.")

        if row.revoked_at is not None:
            if row.revoked_reason != REVOKED_ROTATED:
                raise NotAuthenticated("Session has ended.")
            if now - row.revoked_at <= _ROTATION_GRACE:
                # Two tabs refreshed at once; the other one already holds the new token.
                raise NotAuthenticated("Session was just refreshed.")
            await self._revoke_family(row.family_id, REVOKED_REUSE)
            record_audit(
                self._session,
                self._clock,
                action="auth.refresh_reuse_detected",
                actor_type=ActorType.SYSTEM,
                actor_user_id=row.user_id,
                entity_type="login_session",
                entity_id=row.family_id,
                meta=meta,
            )
            raise RefreshTokenReused("Session was revoked. Please sign in again.")

        if row.expires_at <= now:
            raise NotAuthenticated("Session has expired.")
        user = await self._session.get(User, row.user_id)
        if user is None or not user.is_active:
            raise NotAuthenticated("Session is not valid.")

        row.revoked_at = now
        row.revoked_reason = REVOKED_ROTATED
        row.last_used_at = now
        return await self._issue(user, family_id=row.family_id, rotated_from=row.id, meta=meta)

    async def logout(
        self, *, login_id: uuid.UUID | None, refresh_token: str | None, meta: RequestMeta
    ) -> None:
        """End a login session, identified by either credential. Never fails."""
        row: AuthSession | None = None
        if login_id is not None:
            row = await self._session.scalar(
                select(AuthSession).where(AuthSession.family_id == login_id).limit(1)
            )
        if row is None and refresh_token:
            row = await self._session.scalar(
                select(AuthSession).where(AuthSession.token_hash == hash_token(refresh_token))
            )
        if row is None:
            return
        await self._revoke_family(row.family_id, REVOKED_LOGOUT)
        record_audit(
            self._session,
            self._clock,
            action="auth.logout",
            actor_type=ActorType.USER,
            actor_user_id=row.user_id,
            entity_type="login_session",
            entity_id=row.family_id,
            meta=meta,
        )

    # ------------------------------------------------------------ authentication

    async def authenticate(self, access_token: str) -> tuple[User, uuid.UUID]:
        """Resolve an access token to (user, login session id) or raise NotAuthenticated."""
        now = self._clock.now()
        try:
            claims = decode_access_token(
                access_token, secret=self._settings.jwt_secret.get_secret_value(), now=now
            )
        except InvalidAccessToken as exc:
            raise NotAuthenticated("Sign in to continue.") from exc

        user = await self._session.scalar(
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                User.id == claims.user_id,
                AuthSession.family_id == claims.session_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
            .limit(1)
        )
        if user is None or not user.is_active:
            raise NotAuthenticated("Sign in to continue.")
        return user, claims.session_id

    # --------------------------------------------------------- account management

    async def update_profile(
        self, user: User, *, display_name: str | None, timezone: str | None, meta: RequestMeta
    ) -> User:
        changes: dict[str, dict[str, str | None]] = {}
        if timezone is not None and timezone != user.timezone:
            if not is_valid_timezone(timezone):
                raise ValidationFailed(f"{timezone!r} is not a valid IANA timezone.")
            changes["timezone"] = {"from": user.timezone, "to": timezone}
            user.timezone = timezone
        if display_name is not None and display_name != user.display_name:
            changes["display_name"] = {"from": user.display_name, "to": display_name}
            user.display_name = display_name
        if changes:
            user.updated_at = self._clock.now()
            record_audit(
                self._session,
                self._clock,
                action="account.update",
                actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
                actor_user_id=user.id,
                entity_type="user",
                entity_id=user.id,
                details=changes,
                meta=meta,
            )
            await self._session.flush()
        return user

    async def change_password(
        self,
        user: User,
        *,
        current_password: str,
        new_password: str,
        keep_login_id: uuid.UUID,
        meta: RequestMeta,
    ) -> None:
        """Change the password and sign out every other login session."""
        valid = await anyio.to_thread.run_sync(
            self._passwords.verify, user.password_hash, current_password
        )
        if not valid:
            raise InvalidCredentials("Current password is incorrect.")
        user.password_hash = await anyio.to_thread.run_sync(self._passwords.hash, new_password)
        user.updated_at = self._clock.now()
        await self._session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.family_id != keep_login_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=self._clock.now(), revoked_reason=REVOKED_PASSWORD_CHANGE)
        )
        record_audit(
            self._session,
            self._clock,
            action="account.password_change",
            actor_type=ActorType.ADMIN if user.is_admin else ActorType.USER,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            meta=meta,
        )
        await self._session.flush()

    # ------------------------------------------------------------------ internals

    async def _user_count(self) -> int:
        return int(await self._session.scalar(select(func.count()).select_from(User)) or 0)

    async def _issue(
        self, user: User, *, family_id: uuid.UUID, rotated_from: uuid.UUID | None, meta: RequestMeta
    ) -> IssuedSession:
        now = self._clock.now()
        access_ttl = timedelta(minutes=self._settings.access_token_ttl_minutes)
        refresh_ttl = timedelta(days=self._settings.refresh_token_ttl_days)
        refresh_token = generate_token()
        self._session.add(
            AuthSession(
                user_id=user.id,
                family_id=family_id,
                token_hash=hash_token(refresh_token),
                rotated_from=rotated_from,
                user_agent=meta.user_agent,
                ip_address=meta.ip_address,
                created_at=now,
                expires_at=now + refresh_ttl,
            )
        )
        await self._session.flush()
        access_token = create_access_token(
            user_id=user.id,
            session_id=family_id,
            now=now,
            ttl=access_ttl,
            secret=self._settings.jwt_secret.get_secret_value(),
        )
        return IssuedSession(
            user=user,
            login_id=family_id,
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=now + access_ttl,
            refresh_expires_at=now + refresh_ttl,
        )

    async def _revoke_family(self, family_id: uuid.UUID, reason: str) -> None:
        await self._session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=self._clock.now(), revoked_reason=reason)
        )

    def _audit_login_failure(self, user: User | None, reason: str, meta: RequestMeta) -> None:
        record_audit(
            self._session,
            self._clock,
            action="auth.login_failed",
            actor_type=ActorType.ANONYMOUS,
            actor_user_id=user.id if user else None,
            entity_type="user",
            entity_id=user.id if user else None,
            details={"reason": reason},
            meta=meta,
        )
