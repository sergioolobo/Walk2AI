"""Startup configuration validation and the in-process rate limiter."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.ratelimit import SlidingWindowLimiter

GOOD_SECRET = "0123456789abcdef" * 4


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "jwt_secret": GOOD_SECRET,
        "postgres_password": "a-strong-local-password",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def production(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "cookie_secure": True,
        "public_origin": "https://walk2ai.example",
    }
    values.update(overrides)
    return make_settings(**values)


def test_defaults_match_the_agreed_decisions() -> None:
    settings = make_settings()
    assert settings.default_timezone == "Europe/Lisbon"
    assert settings.registration_mode == "first_user_only"
    assert settings.allow_test_fitness_data is False
    assert settings.sqlalchemy_url.drivername == "postgresql+psycopg"
    assert settings.interactive_docs is True


def test_secrets_are_not_revealed_by_repr() -> None:
    settings = make_settings(openai_api_key="sk-test-value", anthropic_api_key="sk-ant-test")
    rendered = repr(settings) + str(settings.model_dump())
    for secret in (GOOD_SECRET, "a-strong-local-password", "sk-test-value", "sk-ant-test"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"jwt_secret": "too-short"}, "JWT_SECRET must be at least 32"),
        ({"postgres_password": ""}, "POSTGRES_PASSWORD is required"),
        ({"default_timezone": "Europe/Lisboa"}, "not an IANA timezone"),
        ({"database_url": "postgresql+asyncpg://u:p@h/db"}, "postgresql\\+psycopg driver"),
        ({"registration_mode": "anyone"}, "registration_mode"),
        ({"access_token_ttl_minutes": 0}, "access_token_ttl_minutes"),
    ],
)
def test_invalid_configuration_stops_startup(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)


def test_a_database_url_can_replace_the_individual_settings() -> None:
    settings = make_settings(
        postgres_password="",
        database_url="postgresql+psycopg://app:s3cret@db.internal:5432/walk2ai",
    )
    assert settings.sqlalchemy_url.host == "db.internal"
    assert settings.sqlalchemy_url.database == "walk2ai"


def test_production_accepts_a_safe_configuration() -> None:
    settings = production()
    assert settings.interactive_docs is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cookie_secure": False}, "COOKIE_SECURE must be true"),
        ({"public_origin": "http://walk2ai.example"}, "PUBLIC_ORIGIN must be an https"),
        ({"allow_test_fitness_data": True}, "ALLOW_TEST_FITNESS_DATA must be false"),
        ({"registration_mode": "open"}, "REGISTRATION_MODE=open is not allowed"),
        ({"postgres_password": "change-me-locally"}, "POSTGRES_PASSWORD still holds a placeholder"),
        ({"jwt_secret": "change-me-" + "a" * 40}, "JWT_SECRET still holds a placeholder"),
    ],
)
def test_production_refuses_unsafe_configuration(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        production(**overrides)


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_up_to_the_limit_then_reports_the_wait() -> None:
    time = FakeTime()
    limiter = SlidingWindowLimiter(time)
    for _ in range(5):
        assert limiter.hit("login:1.2.3.4", limit=5, window_seconds=60) is None
        time.now += 1
    retry_after = limiter.hit("login:1.2.3.4", limit=5, window_seconds=60)
    assert retry_after == pytest.approx(55)  # the oldest hit leaves the window in 55 s


def test_limiter_window_slides() -> None:
    time = FakeTime()
    limiter = SlidingWindowLimiter(time)
    for _ in range(3):
        assert limiter.hit("k", limit=3, window_seconds=10) is None
    assert limiter.hit("k", limit=3, window_seconds=10) is not None
    time.now += 10.01
    assert limiter.hit("k", limit=3, window_seconds=10) is None


def test_limiter_keys_are_independent_and_refusals_do_not_extend_the_block() -> None:
    time = FakeTime()
    limiter = SlidingWindowLimiter(time)
    assert limiter.hit("a", limit=1, window_seconds=60) is None
    assert limiter.hit("b", limit=1, window_seconds=60) is None
    for _ in range(50):  # hammering while blocked must not push the unblock time out
        time.now += 1
        assert limiter.hit("a", limit=1, window_seconds=60) is not None
    time.now += 10.5
    assert limiter.hit("a", limit=1, window_seconds=60) is None


def test_limiter_reset_and_bounded_memory() -> None:
    time = FakeTime()
    limiter = SlidingWindowLimiter(time)
    limiter.hit("k", limit=1, window_seconds=60)
    limiter.reset()
    assert limiter.hit("k", limit=1, window_seconds=60) is None

    for i in range(25_000):  # a flood of distinct keys must not grow without bound
        limiter.hit(f"flood:{i}", limit=5, window_seconds=60)
    assert len(limiter._hits) <= 20_000
