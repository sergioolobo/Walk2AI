"""Typed application settings.

Environment variables hold infrastructure and secrets only. Business parameters
(reward rates, prices, exchange rates, limits) are versioned rows in the database.

The application refuses to start with an invalid configuration, and is stricter
still when ENVIRONMENT=production.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from zoneinfo import available_timezones

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[3]

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "placeholder", "example")
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- General ---------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    # The application's default timezone: defines when a user's "day" (and its
    # reward cap) rolls over. Stored per user; this is the value new users get.
    default_timezone: str = "Europe/Lisbon"

    # --- PostgreSQL ------------------------------------------------------
    postgres_user: str = "walk2ai"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "walk2ai"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    # Optional full override (e.g. a managed database). Must use the psycopg driver.
    database_url: SecretStr | None = None

    # --- Authentication --------------------------------------------------
    jwt_secret: SecretStr
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    cookie_secure: bool = False
    registration_mode: Literal["first_user_only", "open", "closed"] = "first_user_only"
    pairing_code_ttl_minutes: int = Field(default=10, ge=1, le=60)
    # Argon2id cost parameters (RFC 9106 second recommended option by default).
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost_kib: int = Field(default=65536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)

    # --- Web -------------------------------------------------------------
    public_origin: str = "http://localhost:3000"
    rate_limit_enabled: bool = True
    # Interactive API docs. None = enabled everywhere except production.
    docs_enabled: bool | None = None

    # --- Wallet ----------------------------------------------------------
    reservation_sweep_interval_seconds: int = Field(default=60, ge=5)

    # --- Fitness ---------------------------------------------------------
    # Clearly-labelled test activity, for exercising the reward engine without a
    # device. Never in production.
    allow_test_fitness_data: bool = False
    # How many days back late-arriving activity can still earn credits.
    reward_window_days: int = Field(default=7, ge=1, le=60)
    # How many days back activity is accepted at all (older data is rejected).
    sync_window_days: int = Field(default=30, ge=1, le=366)
    # Plausibility bounds: anything beyond these is rejected, not stored.
    max_steps_per_day: int = Field(default=100_000, ge=1_000)
    max_distance_km_per_day: int = Field(default=500, ge=1)
    max_workout_hours: int = Field(default=12, ge=1, le=24)
    max_days_per_sync: int = Field(default=62, ge=1, le=400)
    max_workouts_per_sync: int = Field(default=500, ge=1, le=5_000)

    # --- AI providers (used from Phase 3; backend only) -------------------
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    ai_request_timeout_seconds: int = Field(default=120, ge=5, le=900)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.default_timezone not in available_timezones():
            raise ValueError(f"DEFAULT_TIMEZONE {self.default_timezone!r} is not an IANA timezone")
        if self.reward_window_days > self.sync_window_days:
            raise ValueError("REWARD_WINDOW_DAYS cannot exceed SYNC_WINDOW_DAYS")

        secret = self.jwt_secret.get_secret_value()
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters "
                "(generate one with: openssl rand -hex 32)"
            )

        if self.database_url is not None:
            driver = make_url(self.database_url.get_secret_value()).drivername
            if driver != "postgresql+psycopg":
                raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        elif not self.postgres_password.get_secret_value():
            raise ValueError("POSTGRES_PASSWORD is required (or set DATABASE_URL)")

        if self.environment == "production":
            self._validate_production(secret)
        return self

    def _validate_production(self, jwt_secret: str) -> None:
        problems: list[str] = []
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if not self.public_origin.startswith("https://"):
            problems.append("PUBLIC_ORIGIN must be an https:// origin")
        if self.allow_test_fitness_data:
            problems.append("ALLOW_TEST_FITNESS_DATA must be false")
        if self.registration_mode == "open":
            problems.append("REGISTRATION_MODE=open is not allowed")
        secrets_to_check = {
            "JWT_SECRET": jwt_secret,
            "POSTGRES_PASSWORD": self.postgres_password.get_secret_value(),
        }
        for name, value in secrets_to_check.items():
            if any(marker in value.lower() for marker in _PLACEHOLDER_MARKERS):
                problems.append(f"{name} still holds a placeholder value")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))

    @property
    def sqlalchemy_url(self) -> URL:
        if self.database_url is not None:
            return make_url(self.database_url.get_secret_value())
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def interactive_docs(self) -> bool:
        if self.docs_enabled is not None:
            return self.docs_enabled
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
