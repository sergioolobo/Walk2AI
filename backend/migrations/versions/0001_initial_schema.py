"""Initial schema.

Creates all 21 tables with their integrity rules. Written as explicit SQL because the
parts that matter most here — CHECK and EXCLUDE constraints, the ledger-immutability
triggers — are exactly the parts schema autogeneration does not understand.

Conventions
  * Walk Credits are BIGINT / INTEGER. Money is NUMERIC(18,8). No floating point.
  * Timestamps are TIMESTAMPTZ (UTC). Activity dates are DATE in the user's calendar.
  * Enumerations are TEXT + CHECK.
  * Versioned rows use half-open [effective_from, effective_until) ranges and cannot
    overlap (EXCLUDE constraints).

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS citext",
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    # ------------------------------------------------------------------ identity
    """
    CREATE TABLE users (
        id              UUID NOT NULL DEFAULT gen_random_uuid(),
        email           CITEXT NOT NULL,
        password_hash   TEXT NOT NULL,
        display_name    TEXT,
        role            TEXT NOT NULL DEFAULT 'user',
        account_status  TEXT NOT NULL DEFAULT 'active',
        timezone        TEXT NOT NULL DEFAULT 'Europe/Lisbon',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_users PRIMARY KEY (id),
        CONSTRAINT uq_users_email UNIQUE (email),
        CONSTRAINT ck_users_role CHECK (role IN ('user', 'admin')),
        CONSTRAINT ck_users_account_status
            CHECK (account_status IN ('active', 'suspended', 'deleted'))
    )
    """,
    """
    CREATE TABLE auth_sessions (
        id              UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id         UUID NOT NULL,
        family_id       UUID NOT NULL,
        token_hash      TEXT NOT NULL,
        rotated_from    UUID,
        user_agent      TEXT,
        ip_address      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at    TIMESTAMPTZ,
        expires_at      TIMESTAMPTZ NOT NULL,
        revoked_at      TIMESTAMPTZ,
        revoked_reason  TEXT,
        CONSTRAINT pk_auth_sessions PRIMARY KEY (id),
        CONSTRAINT uq_auth_sessions_token_hash UNIQUE (token_hash),
        CONSTRAINT fk_auth_sessions_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_auth_sessions_rotated_from_auth_sessions
            FOREIGN KEY (rotated_from) REFERENCES auth_sessions (id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id)",
    "CREATE INDEX ix_auth_sessions_family_id ON auth_sessions (family_id)",
    """
    CREATE TABLE device_tokens (
        id            UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id       UUID NOT NULL,
        name          TEXT NOT NULL,
        platform      TEXT NOT NULL DEFAULT 'ios',
        token_hash    TEXT NOT NULL,
        scopes        TEXT[] NOT NULL DEFAULT ARRAY['fitness:sync'],
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at  TIMESTAMPTZ,
        revoked_at    TIMESTAMPTZ,
        CONSTRAINT pk_device_tokens PRIMARY KEY (id),
        CONSTRAINT uq_device_tokens_token_hash UNIQUE (token_hash),
        CONSTRAINT fk_device_tokens_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_device_tokens_user_id ON device_tokens (user_id)",
    """
    CREATE TABLE device_pairing_codes (
        id               UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id          UUID NOT NULL,
        code_hash        TEXT NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at       TIMESTAMPTZ NOT NULL,
        used_at          TIMESTAMPTZ,
        device_token_id  UUID,
        CONSTRAINT pk_device_pairing_codes PRIMARY KEY (id),
        CONSTRAINT uq_device_pairing_codes_code_hash UNIQUE (code_hash),
        CONSTRAINT fk_device_pairing_codes_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_device_pairing_codes_device_token_id_device_tokens
            FOREIGN KEY (device_token_id) REFERENCES device_tokens (id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX ix_device_pairing_codes_user_id ON device_pairing_codes (user_id)",
    # ------------------------------------------------------------------- fitness
    """
    CREATE TABLE fitness_sync_batches (
        id                 UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id            UUID NOT NULL,
        device_token_id    UUID,
        client_batch_id    UUID NOT NULL,
        source             TEXT NOT NULL,
        is_test_data       BOOLEAN NOT NULL DEFAULT FALSE,
        payload_sha256     TEXT NOT NULL,
        days_received      INTEGER NOT NULL DEFAULT 0,
        workouts_received  INTEGER NOT NULL DEFAULT 0,
        result             JSONB NOT NULL DEFAULT '{}'::jsonb,
        received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_fitness_sync_batches PRIMARY KEY (id),
        CONSTRAINT uq_fitness_sync_batches_user_batch UNIQUE (user_id, client_batch_id),
        CONSTRAINT fk_fitness_sync_batches_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_fitness_sync_batches_device_token_id_device_tokens
            FOREIGN KEY (device_token_id) REFERENCES device_tokens (id) ON DELETE SET NULL,
        CONSTRAINT ck_fitness_sync_batches_source
            CHECK (source IN ('apple_healthkit', 'test', 'manual')),
        CONSTRAINT ck_fitness_sync_batches_test_flag CHECK (is_test_data = (source = 'test')),
        CONSTRAINT ck_fitness_sync_batches_counts
            CHECK (days_received >= 0 AND workouts_received >= 0)
    )
    """,
    """
    CREATE TABLE fitness_daily (
        id                  UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id             UUID NOT NULL,
        activity_date       DATE NOT NULL,
        source              TEXT NOT NULL,
        steps               INTEGER NOT NULL DEFAULT 0,
        walking_distance_m  NUMERIC(12,2),
        running_distance_m  NUMERIC(12,2),
        cycling_distance_m  NUMERIC(12,2),
        active_minutes      INTEGER NOT NULL DEFAULT 0,
        active_energy_kcal  NUMERIC(10,2),
        device_timezone     TEXT,
        is_test_data        BOOLEAN NOT NULL DEFAULT FALSE,
        last_batch_id       UUID,
        synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_fitness_daily PRIMARY KEY (id),
        CONSTRAINT uq_fitness_daily_user_date_source UNIQUE (user_id, activity_date, source),
        CONSTRAINT fk_fitness_daily_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_fitness_daily_last_batch_id_fitness_sync_batches
            FOREIGN KEY (last_batch_id) REFERENCES fitness_sync_batches (id) ON DELETE SET NULL,
        CONSTRAINT ck_fitness_daily_source
            CHECK (source IN ('apple_healthkit', 'test', 'manual')),
        CONSTRAINT ck_fitness_daily_test_flag CHECK (is_test_data = (source = 'test')),
        CONSTRAINT ck_fitness_daily_nonnegative CHECK (
            steps >= 0 AND active_minutes >= 0
            AND COALESCE(walking_distance_m, 0) >= 0
            AND COALESCE(running_distance_m, 0) >= 0
            AND COALESCE(cycling_distance_m, 0) >= 0
            AND COALESCE(active_energy_kcal, 0) >= 0
        )
    )
    """,
    """
    CREATE TABLE workout_events (
        id                     UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id                UUID NOT NULL,
        source                 TEXT NOT NULL,
        external_id            TEXT NOT NULL,
        workout_type           TEXT NOT NULL,
        start_time             TIMESTAMPTZ NOT NULL,
        end_time               TIMESTAMPTZ NOT NULL,
        duration_minutes       NUMERIC(8,2) NOT NULL,
        distance_m             NUMERIC(12,2),
        active_energy_kcal     NUMERIC(10,2),
        origin_bundle_id       TEXT,
        origin_device          TEXT,
        was_user_entered       BOOLEAN NOT NULL DEFAULT FALSE,
        verified               BOOLEAN NOT NULL DEFAULT FALSE,
        is_test_data           BOOLEAN NOT NULL DEFAULT FALSE,
        excluded_from_rewards  BOOLEAN NOT NULL DEFAULT FALSE,
        exclusion_reason       TEXT,
        duplicate_of           UUID,
        deleted_at             TIMESTAMPTZ,
        last_batch_id          UUID,
        created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_workout_events PRIMARY KEY (id),
        CONSTRAINT uq_workout_events_user_source_external
            UNIQUE (user_id, source, external_id),
        CONSTRAINT fk_workout_events_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_workout_events_duplicate_of_workout_events
            FOREIGN KEY (duplicate_of) REFERENCES workout_events (id) ON DELETE SET NULL,
        CONSTRAINT fk_workout_events_last_batch_id_fitness_sync_batches
            FOREIGN KEY (last_batch_id) REFERENCES fitness_sync_batches (id) ON DELETE SET NULL,
        CONSTRAINT ck_workout_events_source
            CHECK (source IN ('apple_healthkit', 'test', 'manual')),
        CONSTRAINT ck_workout_events_test_flag CHECK (is_test_data = (source = 'test')),
        CONSTRAINT ck_workout_events_interval CHECK (end_time > start_time),
        CONSTRAINT ck_workout_events_nonnegative CHECK (
            duration_minutes >= 0
            AND COALESCE(distance_m, 0) >= 0
            AND COALESCE(active_energy_kcal, 0) >= 0
        )
    )
    """,
    "CREATE INDEX ix_workout_events_user_start ON workout_events (user_id, start_time)",
    """
    CREATE TABLE workout_type_rules (
        workout_type  TEXT NOT NULL,
        category      TEXT NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_workout_type_rules PRIMARY KEY (workout_type),
        CONSTRAINT ck_workout_type_rules_category
            CHECK (category IN ('step_based', 'non_step', 'excluded'))
    )
    """,
    # ------------------------------------------------------------------- rewards
    """
    CREATE TABLE reward_rules (
        id               UUID NOT NULL DEFAULT gen_random_uuid(),
        activity_type    TEXT NOT NULL,
        conversion_rate  NUMERIC(12,6),
        daily_limit      INTEGER,
        effective_from   DATE NOT NULL,
        effective_until  DATE,
        enabled          BOOLEAN NOT NULL DEFAULT TRUE,
        created_by       UUID,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_reward_rules PRIMARY KEY (id),
        CONSTRAINT fk_reward_rules_created_by_users
            FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_reward_rules_activity_type
            CHECK (activity_type IN ('steps', 'workout_minutes', 'daily_total')),
        CONSTRAINT ck_reward_rules_nonnegative
            CHECK (COALESCE(conversion_rate, 0) >= 0 AND COALESCE(daily_limit, 0) >= 0),
        CONSTRAINT ck_reward_rules_range
            CHECK (effective_until IS NULL OR effective_until > effective_from),
        CONSTRAINT ck_reward_rules_rate_presence
            CHECK ((activity_type = 'daily_total') = (conversion_rate IS NULL)),
        CONSTRAINT ck_reward_rules_cap_presence
            CHECK (activity_type <> 'daily_total' OR daily_limit IS NOT NULL),
        CONSTRAINT ex_reward_rules_no_overlap EXCLUDE USING gist (
            activity_type WITH =,
            daterange(effective_from, effective_until, '[)') WITH &&
        ) WHERE (enabled)
    )
    """,
    """
    CREATE TABLE daily_rewards (
        id                UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id           UUID NOT NULL,
        activity_date     DATE NOT NULL,
        steps_counted     INTEGER NOT NULL DEFAULT 0,
        minutes_counted   INTEGER NOT NULL DEFAULT 0,
        step_credits      INTEGER NOT NULL DEFAULT 0,
        workout_credits   INTEGER NOT NULL DEFAULT 0,
        entitled_credits  INTEGER NOT NULL DEFAULT 0,
        credits_awarded   INTEGER NOT NULL DEFAULT 0,
        rules_snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_test_data      BOOLEAN NOT NULL DEFAULT FALSE,
        calculated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_daily_rewards PRIMARY KEY (id),
        CONSTRAINT uq_daily_rewards_user_date UNIQUE (user_id, activity_date),
        CONSTRAINT fk_daily_rewards_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT ck_daily_rewards_nonnegative CHECK (
            steps_counted >= 0 AND minutes_counted >= 0 AND step_credits >= 0
            AND workout_credits >= 0 AND entitled_credits >= 0 AND credits_awarded >= 0
        )
    )
    """,
    # -------------------------------------------------------------------- wallet
    """
    CREATE TABLE wallet_accounts (
        id                 UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id            UUID NOT NULL,
        available_balance  BIGINT NOT NULL DEFAULT 0,
        reserved_balance   BIGINT NOT NULL DEFAULT 0,
        lifetime_earned    BIGINT NOT NULL DEFAULT 0,
        lifetime_spent     BIGINT NOT NULL DEFAULT 0,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_wallet_accounts PRIMARY KEY (id),
        CONSTRAINT uq_wallet_accounts_user_id UNIQUE (user_id),
        CONSTRAINT fk_wallet_accounts_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
        CONSTRAINT ck_wallet_accounts_available_nonnegative CHECK (available_balance >= 0),
        CONSTRAINT ck_wallet_accounts_reserved_nonnegative CHECK (reserved_balance >= 0),
        CONSTRAINT ck_wallet_accounts_lifetime_nonnegative
            CHECK (lifetime_earned >= 0 AND lifetime_spent >= 0)
    )
    """,
    """
    CREATE TABLE wallet_reservations (
        id               UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id          UUID NOT NULL,
        wallet_id        UUID NOT NULL,
        request_id       UUID NOT NULL,
        reserved_amount  BIGINT NOT NULL,
        settled_amount   BIGINT,
        released_amount  BIGINT,
        status           TEXT NOT NULL DEFAULT 'active',
        description      TEXT NOT NULL DEFAULT '',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at       TIMESTAMPTZ NOT NULL,
        closed_at        TIMESTAMPTZ,
        CONSTRAINT pk_wallet_reservations PRIMARY KEY (id),
        CONSTRAINT uq_wallet_reservations_request_id UNIQUE (request_id),
        CONSTRAINT fk_wallet_reservations_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
        CONSTRAINT fk_wallet_reservations_wallet_id_wallet_accounts
            FOREIGN KEY (wallet_id) REFERENCES wallet_accounts (id) ON DELETE RESTRICT,
        CONSTRAINT ck_wallet_reservations_status
            CHECK (status IN ('active', 'settled', 'released', 'expired')),
        CONSTRAINT ck_wallet_reservations_amounts CHECK (
            reserved_amount > 0
            AND COALESCE(settled_amount, 0) >= 0
            AND COALESCE(released_amount, 0) >= 0
        ),
        CONSTRAINT ck_wallet_reservations_closed
            CHECK ((status = 'active') = (closed_at IS NULL))
    )
    """,
    """
    CREATE INDEX ix_wallet_reservations_active ON wallet_reservations (expires_at)
        WHERE status = 'active'
    """,
    """
    CREATE INDEX ix_wallet_reservations_user_created
        ON wallet_reservations (user_id, created_at)
    """,
    """
    CREATE TABLE wallet_transactions (
        id                BIGINT GENERATED ALWAYS AS IDENTITY,
        user_id           UUID NOT NULL,
        wallet_id         UUID NOT NULL,
        transaction_type  TEXT NOT NULL,
        amount            BIGINT NOT NULL,
        reserved_delta    BIGINT NOT NULL DEFAULT 0,
        balance_after     BIGINT NOT NULL,
        reserved_after    BIGINT NOT NULL,
        reference_type    TEXT,
        reference_id      UUID,
        idempotency_key   TEXT,
        description       TEXT NOT NULL DEFAULT '',
        metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by        UUID,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_wallet_transactions PRIMARY KEY (id),
        CONSTRAINT fk_wallet_transactions_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
        CONSTRAINT fk_wallet_transactions_wallet_id_wallet_accounts
            FOREIGN KEY (wallet_id) REFERENCES wallet_accounts (id) ON DELETE RESTRICT,
        CONSTRAINT fk_wallet_transactions_created_by_users
            FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,
        CONSTRAINT ck_wallet_transactions_type CHECK (transaction_type IN (
            'ACTIVITY_REWARD', 'REWARD_REVERSAL', 'AI_USAGE',
            'CREDIT_RESERVATION', 'CREDIT_RELEASE',
            'MANUAL_ADJUSTMENT', 'ADMIN_ADJUSTMENT')),
        CONSTRAINT ck_wallet_transactions_balances_nonnegative
            CHECK (balance_after >= 0 AND reserved_after >= 0),
        CONSTRAINT ck_wallet_transactions_shape CHECK (
            CASE transaction_type
                WHEN 'ACTIVITY_REWARD'    THEN amount > 0  AND reserved_delta = 0
                WHEN 'REWARD_REVERSAL'    THEN amount < 0  AND reserved_delta = 0
                WHEN 'CREDIT_RESERVATION' THEN amount < 0  AND reserved_delta = -amount
                WHEN 'CREDIT_RELEASE'     THEN amount > 0  AND reserved_delta = -amount
                WHEN 'AI_USAGE'           THEN amount <= 0 AND reserved_delta <= 0
                                               AND (amount + reserved_delta) < 0
                ELSE amount <> 0 AND reserved_delta = 0
            END
        )
    )
    """,
    """
    CREATE UNIQUE INDEX uq_wallet_transactions_idempotency
        ON wallet_transactions (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    "CREATE INDEX ix_wallet_transactions_user_id ON wallet_transactions (user_id, id)",
    """
    CREATE INDEX ix_wallet_transactions_user_created
        ON wallet_transactions (user_id, created_at)
    """,
    """
    CREATE INDEX ix_wallet_transactions_reference
        ON wallet_transactions (reference_type, reference_id)
    """,
    """
    CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$
    """,
    """
    CREATE TRIGGER trg_wallet_transactions_immutable
        BEFORE UPDATE OR DELETE ON wallet_transactions
        FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()
    """,
    """
    CREATE TRIGGER trg_wallet_transactions_no_truncate
        BEFORE TRUNCATE ON wallet_transactions
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation()
    """,
    """
    CREATE TABLE spending_limits (
        user_id              UUID NOT NULL,
        max_request_wc       INTEGER,
        max_daily_wc         INTEGER,
        max_monthly_wc       INTEGER,
        min_balance_wc       INTEGER NOT NULL DEFAULT 0,
        alert_threshold_pct  SMALLINT NOT NULL DEFAULT 80,
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_spending_limits PRIMARY KEY (user_id),
        CONSTRAINT fk_spending_limits_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT ck_spending_limits_positive CHECK (
            COALESCE(max_request_wc, 1) > 0
            AND COALESCE(max_daily_wc, 1) > 0
            AND COALESCE(max_monthly_wc, 1) > 0
            AND min_balance_wc >= 0
        ),
        CONSTRAINT ck_spending_limits_alert_pct CHECK (alert_threshold_pct BETWEEN 1 AND 100)
    )
    """,
    # --------------------------------------------------------- conversion rates
    """
    CREATE TABLE exchange_rates (
        id               UUID NOT NULL DEFAULT gen_random_uuid(),
        base_currency    TEXT NOT NULL,
        quote_currency   TEXT NOT NULL,
        rate             NUMERIC(18,8) NOT NULL,
        effective_from   TIMESTAMPTZ NOT NULL,
        effective_until  TIMESTAMPTZ,
        source           TEXT NOT NULL DEFAULT 'manual',
        created_by       UUID,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_exchange_rates PRIMARY KEY (id),
        CONSTRAINT fk_exchange_rates_created_by_users
            FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_exchange_rates_currencies CHECK (
            base_currency ~ '^[A-Z]{2,3}$' AND quote_currency ~ '^[A-Z]{2,3}$'
            AND base_currency <> quote_currency
        ),
        CONSTRAINT ck_exchange_rates_rate_positive CHECK (rate > 0),
        CONSTRAINT ck_exchange_rates_range
            CHECK (effective_until IS NULL OR effective_until > effective_from),
        CONSTRAINT ex_exchange_rates_no_overlap EXCLUDE USING gist (
            base_currency WITH =,
            quote_currency WITH =,
            tstzrange(effective_from, effective_until, '[)') WITH &&
        )
    )
    """,
    # ------------------------------------------------------ AI catalogue & chat
    """
    CREATE TABLE ai_models (
        id                         UUID NOT NULL DEFAULT gen_random_uuid(),
        provider                   TEXT NOT NULL,
        model_identifier           TEXT NOT NULL,
        display_name               TEXT NOT NULL,
        context_limit              INTEGER,
        max_output_tokens          INTEGER,
        supports_streaming         BOOLEAN NOT NULL DEFAULT TRUE,
        supports_temperature       BOOLEAN NOT NULL DEFAULT FALSE,
        supports_reasoning_effort  BOOLEAN NOT NULL DEFAULT FALSE,
        enabled                    BOOLEAN NOT NULL DEFAULT FALSE,
        availability_checked_at    TIMESTAMPTZ,
        sort_order                 INTEGER NOT NULL DEFAULT 0,
        notes                      TEXT,
        created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_ai_models PRIMARY KEY (id),
        CONSTRAINT uq_ai_models_provider_identifier UNIQUE (provider, model_identifier),
        CONSTRAINT ck_ai_models_provider CHECK (provider IN ('openai', 'anthropic')),
        CONSTRAINT ck_ai_models_limits_positive
            CHECK (COALESCE(context_limit, 1) > 0 AND COALESCE(max_output_tokens, 1) > 0)
    )
    """,
    """
    CREATE TABLE model_pricing (
        id                         UUID NOT NULL DEFAULT gen_random_uuid(),
        model_id                   UUID NOT NULL,
        input_price                NUMERIC(18,8) NOT NULL,
        output_price               NUMERIC(18,8) NOT NULL,
        cached_input_price         NUMERIC(18,8),
        cache_write_price          NUMERIC(18,8),
        cache_write_1h_price       NUMERIC(18,8),
        long_context_threshold     INTEGER,
        long_context_input_price   NUMERIC(18,8),
        long_context_output_price  NUMERIC(18,8),
        extra_prices               JSONB NOT NULL DEFAULT '{}'::jsonb,
        currency                   TEXT NOT NULL DEFAULT 'USD',
        effective_from             TIMESTAMPTZ NOT NULL,
        effective_until            TIMESTAMPTZ,
        verified                   BOOLEAN NOT NULL DEFAULT FALSE,
        verified_at                TIMESTAMPTZ,
        source_url                 TEXT,
        notes                      TEXT,
        created_by                 UUID,
        created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_model_pricing PRIMARY KEY (id),
        CONSTRAINT fk_model_pricing_model_id_ai_models
            FOREIGN KEY (model_id) REFERENCES ai_models (id) ON DELETE RESTRICT,
        CONSTRAINT fk_model_pricing_created_by_users
            FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_model_pricing_nonnegative CHECK (
            input_price >= 0 AND output_price >= 0
            AND COALESCE(cached_input_price, 0) >= 0
            AND COALESCE(cache_write_price, 0) >= 0
            AND COALESCE(cache_write_1h_price, 0) >= 0
            AND COALESCE(long_context_input_price, 0) >= 0
            AND COALESCE(long_context_output_price, 0) >= 0
            AND COALESCE(long_context_threshold, 1) > 0
        ),
        CONSTRAINT ck_model_pricing_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_model_pricing_range
            CHECK (effective_until IS NULL OR effective_until > effective_from),
        CONSTRAINT ck_model_pricing_verified_at CHECK (verified = (verified_at IS NOT NULL)),
        CONSTRAINT ck_model_pricing_verified_source
            CHECK (NOT verified OR source_url IS NOT NULL),
        CONSTRAINT ck_model_pricing_long_context CHECK (
            (long_context_threshold IS NULL)
            = (long_context_input_price IS NULL AND long_context_output_price IS NULL)
        ),
        CONSTRAINT ex_model_pricing_no_overlap EXCLUDE USING gist (
            model_id WITH =,
            tstzrange(effective_from, effective_until, '[)') WITH &&
        )
    )
    """,
    """
    CREATE TABLE chat_sessions (
        id          UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL,
        title       TEXT NOT NULL DEFAULT 'New conversation',
        provider    TEXT NOT NULL,
        model       TEXT NOT NULL,
        model_id    UUID,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_chat_sessions PRIMARY KEY (id),
        CONSTRAINT fk_chat_sessions_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_chat_sessions_model_id_ai_models
            FOREIGN KEY (model_id) REFERENCES ai_models (id) ON DELETE SET NULL,
        CONSTRAINT ck_chat_sessions_provider CHECK (provider IN ('openai', 'anthropic'))
    )
    """,
    "CREATE INDEX ix_chat_sessions_user_updated ON chat_sessions (user_id, updated_at)",
    """
    CREATE TABLE ai_usage (
        id                   UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id              UUID NOT NULL,
        session_id           UUID,
        request_id           UUID NOT NULL,
        reservation_id       UUID,
        model_id             UUID,
        pricing_id           UUID,
        provider             TEXT NOT NULL,
        model                TEXT NOT NULL,
        input_tokens         INTEGER NOT NULL DEFAULT 0,
        output_tokens        INTEGER NOT NULL DEFAULT 0,
        cached_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens   INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens     INTEGER NOT NULL DEFAULT 0,
        estimated_cost       NUMERIC(18,8),
        provider_cost        NUMERIC(18,8),
        provider_currency    TEXT NOT NULL DEFAULT 'USD',
        exchange_rate        NUMERIC(18,8),
        cost_eur             NUMERIC(18,8),
        wc_per_eur           NUMERIC(18,8),
        credits_reserved     BIGINT NOT NULL DEFAULT 0,
        credits_charged      BIGINT NOT NULL DEFAULT 0,
        cost_basis           TEXT NOT NULL DEFAULT 'none',
        status               TEXT NOT NULL DEFAULT 'pending',
        streamed             BOOLEAN NOT NULL DEFAULT FALSE,
        provider_request_id  TEXT,
        stop_reason          TEXT,
        error_code           TEXT,
        error_message        TEXT,
        duration_ms          INTEGER,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at         TIMESTAMPTZ,
        CONSTRAINT pk_ai_usage PRIMARY KEY (id),
        CONSTRAINT uq_ai_usage_request_id UNIQUE (request_id),
        CONSTRAINT fk_ai_usage_user_id_users
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
        CONSTRAINT fk_ai_usage_session_id_chat_sessions
            FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE SET NULL,
        CONSTRAINT fk_ai_usage_reservation_id_wallet_reservations
            FOREIGN KEY (reservation_id) REFERENCES wallet_reservations (id) ON DELETE RESTRICT,
        CONSTRAINT fk_ai_usage_model_id_ai_models
            FOREIGN KEY (model_id) REFERENCES ai_models (id) ON DELETE RESTRICT,
        CONSTRAINT fk_ai_usage_pricing_id_model_pricing
            FOREIGN KEY (pricing_id) REFERENCES model_pricing (id) ON DELETE RESTRICT,
        CONSTRAINT ck_ai_usage_provider CHECK (provider IN ('openai', 'anthropic')),
        CONSTRAINT ck_ai_usage_cost_basis
            CHECK (cost_basis IN ('provider_usage', 'estimated', 'none')),
        CONSTRAINT ck_ai_usage_status CHECK (status IN (
            'pending', 'succeeded', 'failed', 'cancelled', 'timeout', 'unknown')),
        CONSTRAINT ck_ai_usage_tokens_nonnegative CHECK (
            input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0
            AND cache_write_tokens >= 0 AND reasoning_tokens >= 0
        ),
        CONSTRAINT ck_ai_usage_money_nonnegative CHECK (
            COALESCE(estimated_cost, 0) >= 0 AND COALESCE(provider_cost, 0) >= 0
            AND COALESCE(cost_eur, 0) >= 0 AND COALESCE(exchange_rate, 1) > 0
            AND COALESCE(wc_per_eur, 1) > 0
            AND credits_reserved >= 0 AND credits_charged >= 0
            AND COALESCE(duration_ms, 0) >= 0
        )
    )
    """,
    "CREATE INDEX ix_ai_usage_user_created ON ai_usage (user_id, created_at)",
    "CREATE INDEX ix_ai_usage_session_id ON ai_usage (session_id)",
    """
    CREATE TABLE chat_messages (
        id          UUID NOT NULL DEFAULT gen_random_uuid(),
        session_id  UUID NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'complete',
        usage_id    UUID,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_chat_messages PRIMARY KEY (id),
        CONSTRAINT fk_chat_messages_session_id_chat_sessions
            FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE,
        CONSTRAINT fk_chat_messages_usage_id_ai_usage
            FOREIGN KEY (usage_id) REFERENCES ai_usage (id) ON DELETE SET NULL,
        CONSTRAINT ck_chat_messages_role CHECK (role IN ('system', 'user', 'assistant')),
        CONSTRAINT ck_chat_messages_status CHECK (status IN ('complete', 'partial', 'error'))
    )
    """,
    """
    CREATE INDEX ix_chat_messages_session_created ON chat_messages (session_id, created_at)
    """,
    # --------------------------------------------------------------------- audit
    """
    CREATE TABLE audit_log (
        id             BIGINT GENERATED ALWAYS AS IDENTITY,
        actor_user_id  UUID,
        actor_type     TEXT NOT NULL,
        action         TEXT NOT NULL,
        entity_type    TEXT,
        entity_id      TEXT,
        details        JSONB NOT NULL DEFAULT '{}'::jsonb,
        ip_address     TEXT,
        user_agent     TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT pk_audit_log PRIMARY KEY (id),
        CONSTRAINT fk_audit_log_actor_user_id_users
            FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_audit_log_actor_type
            CHECK (actor_type IN ('user', 'admin', 'device', 'system', 'anonymous'))
    )
    """,
    "CREATE INDEX ix_audit_log_created_at ON audit_log (created_at)",
    "CREATE INDEX ix_audit_log_entity ON audit_log (entity_type, entity_id)",
    # The audit log is append-only, with one exception the schema itself requires:
    # deleting a user sets actor_user_id to NULL (ON DELETE SET NULL). That single
    # column may change to NULL; nothing else may change and no row may be removed.
    """
    CREATE FUNCTION forbid_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'UPDATE'
           AND NEW.actor_user_id IS NULL
           AND (to_jsonb(NEW) - 'actor_user_id') = (to_jsonb(OLD) - 'actor_user_id') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$
    """,
    """
    CREATE TRIGGER trg_audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()
    """,
    """
    CREATE TRIGGER trg_audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_mutation()
    """,
)

# Reverse dependency order.
TABLES_IN_DROP_ORDER: tuple[str, ...] = (
    "audit_log",
    "chat_messages",
    "ai_usage",
    "chat_sessions",
    "model_pricing",
    "ai_models",
    "exchange_rates",
    "spending_limits",
    "wallet_transactions",
    "wallet_reservations",
    "wallet_accounts",
    "daily_rewards",
    "reward_rules",
    "workout_type_rules",
    "workout_events",
    "fitness_daily",
    "fitness_sync_batches",
    "device_pairing_codes",
    "device_tokens",
    "auth_sessions",
    "users",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION forbid_audit_mutation()")
    op.execute("DROP FUNCTION forbid_ledger_mutation()")
    # Extensions are left installed: they are harmless and may be shared.
