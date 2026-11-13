-- =============================================================================
-- Walk2AI — reference database schema (Phase 0 design artefact)
--
-- Target: PostgreSQL 16+.
-- Status: DESIGN REFERENCE. From Phase 1 onward the source of truth is the
--         SQLAlchemy models + Alembic migrations in backend/. The first Alembic
--         migration is derived from this file; after that this file is only
--         regenerated (pg_dump --schema-only) for documentation.
--
-- Conventions
--   * Walk Credits (WC) are BIGINT/INTEGER. Never floating point.
--   * Real money (provider cost, prices, FX rates) is NUMERIC(18,8).
--   * All timestamps are TIMESTAMPTZ (UTC). "Activity dates" are DATE in the
--     user's local calendar, as reported by the device.
--   * Enumerations are TEXT + CHECK (easier to migrate than native enums).
--   * Versioned configuration (reward rules, prices, FX) uses half-open
--     [effective_from, effective_until) ranges; overlaps are rejected by
--     EXCLUDE constraints, so "which rule applied on day X" is unambiguous.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive e-mail
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- EXCLUDE constraints on scalar + range

-- -----------------------------------------------------------------------------
-- Identity & access
-- -----------------------------------------------------------------------------
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           CITEXT NOT NULL UNIQUE,
    password_hash   TEXT   NOT NULL,                      -- Argon2id
    display_name    TEXT,
    role            TEXT   NOT NULL DEFAULT 'user'
                    CHECK (role IN ('user', 'admin')),
    account_status  TEXT   NOT NULL DEFAULT 'active'
                    CHECK (account_status IN ('active', 'suspended', 'deleted')),
    timezone        TEXT   NOT NULL DEFAULT 'UTC',        -- IANA name; defines the user's "day"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Browser sessions (rotating refresh tokens). Only a hash of the token is stored.
CREATE TABLE auth_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,                   -- SHA-256 of the refresh token
    rotated_from  UUID REFERENCES auth_sessions(id) ON DELETE SET NULL,
    user_agent    TEXT,
    ip_address    INET,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX ix_auth_sessions_user ON auth_sessions (user_id) WHERE revoked_at IS NULL;

-- Long-lived, narrowly scoped tokens for the iOS companion. A device token can
-- push fitness data; it can never spend credits, chat, or reach admin routes.
CREATE TABLE device_tokens (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,                          -- e.g. "iPhone 17 Pro"
    platform      TEXT NOT NULL DEFAULT 'ios',
    token_hash    TEXT NOT NULL UNIQUE,                   -- SHA-256 of the device token
    scopes        TEXT[] NOT NULL DEFAULT ARRAY['fitness:sync'],
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX ix_device_tokens_user ON device_tokens (user_id) WHERE revoked_at IS NULL;

-- -----------------------------------------------------------------------------
-- Fitness ingestion
-- -----------------------------------------------------------------------------
-- One row per sync call. (user_id, client_batch_id) makes POST /fitness/sync
-- idempotent: a retried batch returns the stored result instead of re-running.
CREATE TABLE fitness_sync_batches (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_token_id  UUID REFERENCES device_tokens(id) ON DELETE SET NULL,
    client_batch_id  UUID NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('apple_healthkit', 'test', 'manual')),
    is_test_data     BOOLEAN NOT NULL DEFAULT FALSE,
    payload_sha256   TEXT NOT NULL,
    days_received    INTEGER NOT NULL DEFAULT 0 CHECK (days_received >= 0),
    workouts_received INTEGER NOT NULL DEFAULT 0 CHECK (workouts_received >= 0),
    result           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- accepted / rejected / rewards summary
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, client_batch_id),
    CHECK (is_test_data = (source = 'test'))
);

-- Daily aggregates. For apple_healthkit the values are HealthKit *statistics*
-- (already de-duplicated across iPhone / Mi Fitness by HealthKit), never a sum
-- of raw samples. A re-sync of the same day overwrites the row (latest wins).
CREATE TABLE fitness_daily (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_date        DATE NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('apple_healthkit', 'test', 'manual')),
    steps                INTEGER NOT NULL DEFAULT 0 CHECK (steps >= 0),
    walking_distance_m   NUMERIC(12,2) CHECK (walking_distance_m >= 0),  -- derived, see FITNESS_INTEGRATION.md
    running_distance_m   NUMERIC(12,2) CHECK (running_distance_m >= 0),  -- from running workouts
    cycling_distance_m   NUMERIC(12,2) CHECK (cycling_distance_m >= 0),
    active_minutes       INTEGER NOT NULL DEFAULT 0 CHECK (active_minutes >= 0), -- server-computed, reward-eligible
    active_energy_kcal   NUMERIC(10,2) CHECK (active_energy_kcal >= 0),
    device_timezone      TEXT,
    is_test_data         BOOLEAN NOT NULL DEFAULT FALSE,
    last_batch_id        UUID REFERENCES fitness_sync_batches(id) ON DELETE SET NULL,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, activity_date, source),
    CHECK (is_test_data = (source = 'test'))
);
CREATE INDEX ix_fitness_daily_user_date ON fitness_daily (user_id, activity_date DESC);

CREATE TABLE workout_events (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source                TEXT NOT NULL CHECK (source IN ('apple_healthkit', 'test', 'manual')),
    external_id           TEXT NOT NULL,                  -- HKWorkout UUID
    workout_type          TEXT NOT NULL,                  -- normalised, e.g. 'traditional_strength_training'
    start_time            TIMESTAMPTZ NOT NULL,
    end_time              TIMESTAMPTZ NOT NULL,
    duration_minutes      NUMERIC(8,2) NOT NULL CHECK (duration_minutes >= 0), -- active time, excludes pauses
    distance_m            NUMERIC(12,2) CHECK (distance_m >= 0),
    active_energy_kcal    NUMERIC(10,2) CHECK (active_energy_kcal >= 0),
    origin_bundle_id      TEXT,                           -- app that wrote it to HealthKit (e.g. Mi Fitness)
    origin_device         TEXT,
    was_user_entered      BOOLEAN NOT NULL DEFAULT FALSE,
    verified              BOOLEAN NOT NULL DEFAULT FALSE, -- passed server-side validation
    is_test_data          BOOLEAN NOT NULL DEFAULT FALSE,
    excluded_from_rewards BOOLEAN NOT NULL DEFAULT FALSE, -- manual exclusion
    exclusion_reason      TEXT,
    duplicate_of          UUID REFERENCES workout_events(id) ON DELETE SET NULL,
    deleted_at            TIMESTAMPTZ,                    -- deleted in HealthKit (soft delete)
    last_batch_id         UUID REFERENCES fitness_sync_batches(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, source, external_id),
    CHECK (end_time > start_time),
    CHECK (is_test_data = (source = 'test'))
);
CREATE INDEX ix_workout_events_user_start ON workout_events (user_id, start_time DESC);

-- How each workout type is treated by the reward engine.
--   step_based : already paid through steps (walking, running, hiking) -> 0 extra minutes
--   non_step   : earns workout minutes (strength, cycling, swimming, ...)
--   excluded   : stored, never rewarded
CREATE TABLE workout_type_rules (
    workout_type  TEXT PRIMARY KEY,
    category      TEXT NOT NULL CHECK (category IN ('step_based', 'non_step', 'excluded')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Reward engine
-- -----------------------------------------------------------------------------
-- Versioned rules. Rules are matched by ACTIVITY DATE, so changing a rate never
-- alters days that fall before the new rule's effective_from.
--   steps           : conversion_rate = WC per step             (0.1)
--   workout_minutes : conversion_rate = WC per eligible minute  (5)
--   daily_total     : no rate; daily_limit = cap on the day's total (1500)
CREATE TABLE reward_rules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_type    TEXT NOT NULL CHECK (activity_type IN ('steps', 'workout_minutes', 'daily_total')),
    conversion_rate  NUMERIC(12,6) CHECK (conversion_rate >= 0),
    daily_limit      INTEGER CHECK (daily_limit >= 0),    -- WC; per-activity cap, or the total cap
    effective_from   DATE NOT NULL,
    effective_until  DATE,                                -- exclusive; NULL = open-ended
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    CHECK ((activity_type = 'daily_total') = (conversion_rate IS NULL)),
    CHECK (activity_type <> 'daily_total' OR daily_limit IS NOT NULL),
    EXCLUDE USING gist (
        activity_type WITH =,
        daterange(effective_from, effective_until, '[)') WITH &&
    ) WHERE (enabled)
);

-- The reward ledger: one row per user per day. credits_awarded is the amount
-- actually paid so far for that day and only ever grows through the engine
-- (it shrinks only via an explicit REWARD_REVERSAL). Re-running a sync pays
-- max(0, entitled_credits - credits_awarded): idempotent by construction.
CREATE TABLE daily_rewards (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_date      DATE NOT NULL,
    steps_counted      INTEGER NOT NULL DEFAULT 0 CHECK (steps_counted >= 0),
    minutes_counted    INTEGER NOT NULL DEFAULT 0 CHECK (minutes_counted >= 0),
    step_credits       INTEGER NOT NULL DEFAULT 0 CHECK (step_credits >= 0),
    workout_credits    INTEGER NOT NULL DEFAULT 0 CHECK (workout_credits >= 0),
    entitled_credits   INTEGER NOT NULL DEFAULT 0 CHECK (entitled_credits >= 0),  -- after caps
    credits_awarded    INTEGER NOT NULL DEFAULT 0 CHECK (credits_awarded >= 0),   -- paid to the wallet
    rules_snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,   -- rule ids + rates used
    is_test_data       BOOLEAN NOT NULL DEFAULT FALSE,
    calculated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, activity_date)
);

-- -----------------------------------------------------------------------------
-- Wallet
-- -----------------------------------------------------------------------------
CREATE TABLE wallet_accounts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    available_balance  BIGINT NOT NULL DEFAULT 0 CHECK (available_balance >= 0),
    reserved_balance   BIGINT NOT NULL DEFAULT 0 CHECK (reserved_balance  >= 0),
    lifetime_earned    BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_earned   >= 0),
    lifetime_spent     BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_spent    >= 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE wallet_reservations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    wallet_id        UUID NOT NULL REFERENCES wallet_accounts(id) ON DELETE RESTRICT,
    request_id       UUID NOT NULL UNIQUE,                -- the AI request this reservation backs
    reserved_amount  BIGINT NOT NULL CHECK (reserved_amount > 0),
    settled_amount   BIGINT CHECK (settled_amount  >= 0), -- charged on success
    released_amount  BIGINT CHECK (released_amount >= 0), -- returned to available
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'settled', 'released', 'expired')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    closed_at        TIMESTAMPTZ,
    CHECK ((status = 'active') = (closed_at IS NULL))
);
CREATE INDEX ix_wallet_reservations_active ON wallet_reservations (expires_at) WHERE status = 'active';
CREATE INDEX ix_wallet_reservations_user   ON wallet_reservations (user_id, created_at DESC);

-- Immutable ledger. Every change to a wallet balance is exactly one row here,
-- written in the same DB transaction as the balance update.
--   amount         : signed change to available_balance
--   reserved_delta : signed change to reserved_balance
-- Invariants (asserted by tests and by the reconciliation job):
--   wallet.available_balance = SUM(amount)          over the wallet's rows
--   wallet.reserved_balance  = SUM(reserved_delta)  over the wallet's rows
CREATE TABLE wallet_transactions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- strict ordering
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    wallet_id         UUID NOT NULL REFERENCES wallet_accounts(id) ON DELETE RESTRICT,
    transaction_type  TEXT NOT NULL CHECK (transaction_type IN (
                          'ACTIVITY_REWARD', 'REWARD_REVERSAL', 'AI_USAGE',
                          'CREDIT_RESERVATION', 'CREDIT_RELEASE',
                          'MANUAL_ADJUSTMENT', 'ADMIN_ADJUSTMENT')),
    amount            BIGINT NOT NULL,
    reserved_delta    BIGINT NOT NULL DEFAULT 0,
    balance_after     BIGINT NOT NULL CHECK (balance_after  >= 0),      -- available after this row
    reserved_after    BIGINT NOT NULL CHECK (reserved_after >= 0),
    reference_type    TEXT,                                -- 'daily_reward' | 'reservation' | 'ai_usage' | ...
    reference_id      UUID,
    idempotency_key   TEXT,
    description       TEXT NOT NULL DEFAULT '',
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Shape of each transaction type:
    CONSTRAINT ck_wallet_tx_shape CHECK (
        CASE transaction_type
            WHEN 'ACTIVITY_REWARD'    THEN amount > 0  AND reserved_delta = 0
            WHEN 'REWARD_REVERSAL'    THEN amount < 0  AND reserved_delta = 0
            WHEN 'CREDIT_RESERVATION' THEN amount < 0  AND reserved_delta = -amount
            WHEN 'CREDIT_RELEASE'     THEN amount > 0  AND reserved_delta = -amount
            WHEN 'AI_USAGE'           THEN amount <= 0 AND reserved_delta <= 0
                                           AND (amount + reserved_delta) < 0
            ELSE amount <> 0 AND reserved_delta = 0        -- manual / admin adjustments
        END
    )
);
CREATE UNIQUE INDEX uq_wallet_tx_idempotency ON wallet_transactions (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX ix_wallet_tx_user_created ON wallet_transactions (user_id, id DESC);
CREATE INDEX ix_wallet_tx_reference    ON wallet_transactions (reference_type, reference_id);

CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER trg_wallet_transactions_immutable
    BEFORE UPDATE OR DELETE ON wallet_transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
CREATE TRIGGER trg_wallet_transactions_no_truncate
    BEFORE TRUNCATE ON wallet_transactions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation();

-- Per-user spending controls (all in WC). NULL = no limit of that kind.
CREATE TABLE spending_limits (
    user_id              UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    max_request_wc       INTEGER CHECK (max_request_wc  > 0),
    max_daily_wc         INTEGER CHECK (max_daily_wc    > 0),
    max_monthly_wc       INTEGER CHECK (max_monthly_wc  > 0),
    min_balance_wc       INTEGER NOT NULL DEFAULT 0 CHECK (min_balance_wc >= 0),
    alert_threshold_pct  SMALLINT NOT NULL DEFAULT 80 CHECK (alert_threshold_pct BETWEEN 1 AND 100),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Conversion rates (versioned): USD->EUR for provider billing, EUR->WC for the
-- internal economy (1,000 WC = EUR 0.10  =>  EUR->WC rate = 10000).
-- -----------------------------------------------------------------------------
CREATE TABLE exchange_rates (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    base_currency    TEXT NOT NULL CHECK (base_currency  ~ '^[A-Z]{2,3}$'),
    quote_currency   TEXT NOT NULL CHECK (quote_currency ~ '^[A-Z]{2,3}$'),
    rate             NUMERIC(18,8) NOT NULL CHECK (rate > 0),  -- 1 base = rate quote
    effective_from   TIMESTAMPTZ NOT NULL,
    effective_until  TIMESTAMPTZ,
    source           TEXT NOT NULL DEFAULT 'manual',
    created_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (base_currency <> quote_currency),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    EXCLUDE USING gist (
        base_currency WITH =, quote_currency WITH =,
        tstzrange(effective_from, effective_until, '[)') WITH &&
    )
);

-- -----------------------------------------------------------------------------
-- AI catalogue & pricing
-- -----------------------------------------------------------------------------
CREATE TABLE ai_models (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider                  TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    model_identifier          TEXT NOT NULL,              -- exact API model id
    display_name              TEXT NOT NULL,
    context_limit             INTEGER CHECK (context_limit > 0),
    max_output_tokens         INTEGER CHECK (max_output_tokens > 0),
    supports_streaming        BOOLEAN NOT NULL DEFAULT TRUE,
    supports_temperature      BOOLEAN NOT NULL DEFAULT FALSE,
    supports_reasoning_effort BOOLEAN NOT NULL DEFAULT FALSE,
    enabled                   BOOLEAN NOT NULL DEFAULT FALSE,  -- off until availability + price verified
    availability_checked_at   TIMESTAMPTZ,                -- last successful check against the provider
    sort_order                INTEGER NOT NULL DEFAULT 0,
    notes                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, model_identifier)
);

-- Prices are per 1,000,000 tokens, in `currency`. A model can only be used for
-- chat while it has a VERIFIED price row covering "now".
CREATE TABLE model_pricing (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id                     UUID NOT NULL REFERENCES ai_models(id) ON DELETE RESTRICT,
    input_price                  NUMERIC(18,8) NOT NULL CHECK (input_price  >= 0),
    output_price                 NUMERIC(18,8) NOT NULL CHECK (output_price >= 0),
    cached_input_price           NUMERIC(18,8) CHECK (cached_input_price   >= 0),  -- cache read
    cache_write_price            NUMERIC(18,8) CHECK (cache_write_price    >= 0),  -- short-TTL cache write
    cache_write_1h_price         NUMERIC(18,8) CHECK (cache_write_1h_price >= 0),  -- long-TTL cache write
    -- Optional long-prompt tier (some models charge more above a prompt size).
    long_context_threshold       INTEGER CHECK (long_context_threshold > 0),
    long_context_input_price     NUMERIC(18,8) CHECK (long_context_input_price  >= 0),
    long_context_output_price    NUMERIC(18,8) CHECK (long_context_output_price >= 0),
    extra_prices                 JSONB NOT NULL DEFAULT '{}'::jsonb,  -- any other billable category
    currency                     TEXT NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
    effective_from               TIMESTAMPTZ NOT NULL,
    effective_until              TIMESTAMPTZ,
    verified                     BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at                  TIMESTAMPTZ,
    source_url                   TEXT,                    -- provider pricing page used
    notes                        TEXT,
    created_by                   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    CHECK (verified = (verified_at IS NOT NULL)),
    CHECK (NOT verified OR source_url IS NOT NULL),
    CHECK ((long_context_threshold IS NULL)
           = (long_context_input_price IS NULL AND long_context_output_price IS NULL)),
    EXCLUDE USING gist (
        model_id WITH =,
        tstzrange(effective_from, effective_until, '[)') WITH &&
    )
);

-- -----------------------------------------------------------------------------
-- Chat
-- -----------------------------------------------------------------------------
CREATE TABLE chat_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'New conversation',
    provider    TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    model       TEXT NOT NULL,                            -- model_identifier last used
    model_id    UUID REFERENCES ai_models(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_sessions_user ON chat_sessions (user_id, updated_at DESC);

-- -----------------------------------------------------------------------------
-- AI usage: the financial record of every provider call. Survives deletion of
-- the conversation it belonged to (session_id is SET NULL, content is not here).
-- -----------------------------------------------------------------------------
CREATE TABLE ai_usage (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    session_id           UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    request_id           UUID NOT NULL UNIQUE,
    reservation_id       UUID REFERENCES wallet_reservations(id) ON DELETE RESTRICT,
    model_id             UUID REFERENCES ai_models(id) ON DELETE RESTRICT,
    pricing_id           UUID REFERENCES model_pricing(id) ON DELETE RESTRICT,  -- price version applied
    provider             TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    model                TEXT NOT NULL,
    input_tokens         INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens       >= 0),  -- uncached input
    output_tokens        INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens      >= 0),  -- includes reasoning
    cached_tokens        INTEGER NOT NULL DEFAULT 0 CHECK (cached_tokens      >= 0),  -- cache reads
    cache_write_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    reasoning_tokens     INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens   >= 0),  -- subset of output, if reported
    estimated_cost       NUMERIC(18,8) CHECK (estimated_cost >= 0),   -- worst case before the call
    provider_cost        NUMERIC(18,8) CHECK (provider_cost  >= 0),   -- actual, in provider_currency
    provider_currency    TEXT NOT NULL DEFAULT 'USD',
    exchange_rate        NUMERIC(18,8) CHECK (exchange_rate > 0),     -- provider_currency -> EUR used
    cost_eur             NUMERIC(18,8) CHECK (cost_eur >= 0),
    wc_per_eur           NUMERIC(18,8) CHECK (wc_per_eur > 0),        -- EUR -> WC rate used
    credits_reserved     BIGINT NOT NULL DEFAULT 0 CHECK (credits_reserved >= 0),
    credits_charged      BIGINT NOT NULL DEFAULT 0 CHECK (credits_charged  >= 0),
    cost_basis           TEXT NOT NULL DEFAULT 'none'
                         CHECK (cost_basis IN ('provider_usage', 'estimated', 'none')),
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'succeeded', 'failed',
                                           'cancelled', 'timeout', 'unknown')),
    streamed             BOOLEAN NOT NULL DEFAULT FALSE,
    provider_request_id  TEXT,
    stop_reason          TEXT,
    error_code           TEXT,
    error_message        TEXT,
    duration_ms          INTEGER CHECK (duration_ms >= 0),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);
CREATE INDEX ix_ai_usage_user_created ON ai_usage (user_id, created_at DESC);
CREATE INDEX ix_ai_usage_session      ON ai_usage (session_id);

CREATE TABLE chat_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'complete' CHECK (status IN ('complete', 'partial', 'error')),
    usage_id    UUID REFERENCES ai_usage(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_messages_session ON chat_messages (session_id, created_at);

-- -----------------------------------------------------------------------------
-- Audit log (append-only): logins, admin changes, rule/price edits, adjustments,
-- device pairing, data deletion.
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'admin', 'device', 'system')),
    action         TEXT NOT NULL,                          -- e.g. 'reward_rule.create'
    entity_type    TEXT,
    entity_id      TEXT,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,     -- before/after, never secrets
    ip_address     INET,
    user_agent     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_log_created ON audit_log (created_at DESC);
CREATE INDEX ix_audit_log_entity  ON audit_log (entity_type, entity_id);

CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
