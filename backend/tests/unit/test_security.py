"""Password hashing, access tokens, opaque tokens and pairing codes."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security import (
    DEVICE_TOKEN_PREFIX,
    PAIRING_ALPHABET,
    PAIRING_CODE_LENGTH,
    InvalidAccessToken,
    PasswordService,
    create_access_token,
    decode_access_token,
    format_pairing_code,
    generate_pairing_code,
    generate_token,
    hash_pairing_code,
    hash_token,
    normalise_pairing_code,
)

SECRET = "unit-test-secret-" + "x" * 32
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=15)


@pytest.fixture(scope="module")
def passwords() -> PasswordService:
    return PasswordService(time_cost=1, memory_cost_kib=8, parallelism=1)


def test_password_hash_is_argon2id_and_verifies(passwords: PasswordService) -> None:
    hashed = passwords.hash("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert "correct horse" not in hashed
    assert passwords.verify(hashed, "correct horse battery staple")
    assert not passwords.verify(hashed, "correct horse battery stapler")
    assert not passwords.verify(hashed, "")


def test_same_password_hashes_differently_each_time(passwords: PasswordService) -> None:
    assert passwords.hash("same password twice") != passwords.hash("same password twice")


def test_verify_never_raises_on_garbage(passwords: PasswordService) -> None:
    assert not passwords.verify("not-a-hash", "anything")
    assert not passwords.verify("", "anything")
    passwords.verify_decoy("anything")  # must simply return


def test_rehash_is_requested_when_parameters_get_stronger(passwords: PasswordService) -> None:
    hashed = passwords.hash("a password to upgrade")
    assert not passwords.needs_rehash(hashed)
    stronger = PasswordService(time_cost=2, memory_cost_kib=16, parallelism=1)
    assert stronger.needs_rehash(hashed)


def test_access_token_round_trip() -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(
        user_id=user_id, session_id=session_id, now=NOW, ttl=TTL, secret=SECRET
    )
    claims = decode_access_token(token, secret=SECRET, now=NOW + timedelta(minutes=14))
    assert (claims.user_id, claims.session_id) == (user_id, session_id)
    assert claims.expires_at == NOW + TTL


def test_access_token_expires_by_the_injected_clock() -> None:
    token = create_access_token(
        user_id=uuid.uuid4(), session_id=uuid.uuid4(), now=NOW, ttl=TTL, secret=SECRET
    )
    decode_access_token(token, secret=SECRET, now=NOW + TTL - timedelta(seconds=1))
    with pytest.raises(InvalidAccessToken, match="expired"):
        decode_access_token(token, secret=SECRET, now=NOW + TTL)


def _forged(**overrides: object) -> str:
    payload: dict[str, object] = {
        "iss": "walk2ai",
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + TTL).timestamp()),
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not.a.jwt",
        "a" * 200,
        _forged(iss="someone-else"),
        _forged(sub="not-a-uuid"),
        _forged(sid=None),
        _forged(exp=None),
        _forged(sub=None),
        jwt.encode({"sub": str(uuid.uuid4())}, "another-secret-" + "y" * 32, algorithm="HS256"),
    ],
)
def test_malformed_or_foreign_tokens_are_rejected(token: str) -> None:
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token, secret=SECRET, now=NOW)


def test_token_signed_with_a_different_key_is_rejected() -> None:
    token = create_access_token(
        user_id=uuid.uuid4(), session_id=uuid.uuid4(), now=NOW, ttl=TTL, secret="k" * 40
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token, secret=SECRET, now=NOW)


def test_unsigned_token_is_rejected() -> None:
    """The classic 'alg: none' downgrade must not be accepted."""
    import base64
    import json

    def segment(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {
        "iss": "walk2ai",
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "exp": int((NOW + TTL).timestamp()),
    }
    unsigned = f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(payload)}."
    with pytest.raises(InvalidAccessToken):
        decode_access_token(unsigned, secret=SECRET, now=NOW)


def test_opaque_tokens_are_long_unique_and_stored_only_as_hashes() -> None:
    tokens = {generate_token(DEVICE_TOKEN_PREFIX) for _ in range(200)}
    assert len(tokens) == 200
    token = next(iter(tokens))
    assert token.startswith("w2a_dev_")
    assert len(token) >= 43 + len(DEVICE_TOKEN_PREFIX)  # 32 random bytes, base64url
    digest = hash_token(token)
    assert len(digest) == 64
    assert token not in digest
    assert hash_token(token) == digest
    assert hash_token(token + "x") != digest


def test_pairing_codes_use_the_unambiguous_alphabet() -> None:
    codes = [generate_pairing_code() for _ in range(300)]
    assert all(len(code) == PAIRING_CODE_LENGTH for code in codes)
    assert all(set(code) <= set(PAIRING_ALPHABET) for code in codes)
    assert not set("0O1ILU") & set(PAIRING_ALPHABET)
    assert len(set(codes)) > 295


def test_pairing_code_is_forgiving_about_how_it_is_typed() -> None:
    assert format_pairing_code("ABCDEFGH") == "ABCD-EFGH"
    assert normalise_pairing_code(" abcd-efgh ") == "ABCDEFGH"
    reference = hash_pairing_code("ABCDEFGH", secret=SECRET)
    assert hash_pairing_code("abcd-efgh", secret=SECRET) == reference
    assert hash_pairing_code("ABCD EFGH", secret=SECRET) == reference
    assert hash_pairing_code("ABCDEFGJ", secret=SECRET) != reference


def test_pairing_code_hash_depends_on_the_server_secret() -> None:
    """A leaked database alone is not enough to test guesses against stored codes."""
    assert hash_pairing_code("ABCDEFGH", secret=SECRET) != hash_pairing_code(
        "ABCDEFGH", secret="another-secret-" + "z" * 32
    )
