"""Password hashing, tokens and pairing codes.

Cryptography comes from established libraries only: Argon2id via argon2-cffi, JWT via
PyJWT, randomness via ``secrets``. Nothing here is a home-grown algorithm.

Storage rules
  * Passwords: Argon2id hashes.
  * Refresh and device tokens: 256 bits of randomness, stored as SHA-256. A fast hash
    is appropriate because the input is already high-entropy.
  * Pairing codes: short and typed by a human, so they are stored as an HMAC keyed
    with the server secret, expire quickly and work once.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ACCESS_TOKEN_ALGORITHM = "HS256"  # noqa: S105 - algorithm name
TOKEN_ISSUER = "walk2ai"  # noqa: S105 - issuer name
DEVICE_TOKEN_PREFIX = "w2a_dev_"  # noqa: S105 - public prefix

# No 0/O, 1/I/L or U: unambiguous when read off a screen and typed on a phone.
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
PAIRING_CODE_LENGTH = 8


class PasswordService:
    def __init__(self, *, time_cost: int, memory_cost_kib: int, parallelism: int) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
        )
        # Verified against when the account does not exist, so that an unknown
        # e-mail costs the same time as a wrong password.
        self._decoy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def verify_decoy(self, password: str) -> None:
        self.verify(self._decoy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


class InvalidAccessToken(Exception):
    """The access token is malformed, wrongly signed or expired."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def create_access_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime, ttl: timedelta, secret: str
) -> str:
    payload = {
        "iss": TOKEN_ISSUER,
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ACCESS_TOKEN_ALGORITHM)


def decode_access_token(token: str, *, secret: str, now: datetime) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ACCESS_TOKEN_ALGORITHM],
            issuer=TOKEN_ISSUER,
            # Expiry is checked below against the injected clock, not the wall clock.
            options={"verify_exp": False, "verify_iat": False, "require": ["exp", "sub", "sid"]},
        )
        claims = AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        raise InvalidAccessToken("invalid access token") from exc
    if now >= claims.expires_at:
        raise InvalidAccessToken("access token expired")
    return claims


def generate_token(prefix: str = "") -> str:
    """A new opaque bearer credential with 256 bits of entropy."""
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def normalise_pairing_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_pairing_code(code: str) -> str:
    half = PAIRING_CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


def hash_pairing_code(code: str, *, secret: str) -> str:
    key = hashlib.sha256(b"walk2ai:pairing-code:" + secret.encode("utf-8")).digest()
    return hmac.new(key, normalise_pairing_code(code).encode("utf-8"), hashlib.sha256).hexdigest()
