-- =============================================================================
-- Walk2AI — database schema (GENERATED, do not edit)
--
-- This file is documentation. It is a schema-only pg_dump of a database built by
-- the Alembic migrations in backend/migrations/ on PostgreSQL 16.
-- The migrations are the source of truth; regenerate this file after adding one:
--
--     make schema-doc
--
-- Conventions
--   * Walk Credits are BIGINT / INTEGER. Money is NUMERIC(18,8). No floating point.
--   * Timestamps are TIMESTAMPTZ, stored in UTC. Activity dates are DATE in the
--     user's local calendar (Europe/Lisbon by default).
--   * Enumerations are TEXT + CHECK.
--   * Versioned rows use half-open [effective_from, effective_until) ranges that
--     cannot overlap (EXCLUDE constraints).
--   * wallet_transactions and audit_log are append-only (triggers).
-- =============================================================================

--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';

--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;

--
-- Name: EXTENSION citext; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION citext IS 'data type for case-insensitive character strings';

--
-- Name: forbid_audit_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_audit_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE'
           AND NEW.actor_user_id IS NULL
           AND (to_jsonb(NEW) - 'actor_user_id') = (to_jsonb(OLD) - 'actor_user_id') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$;

--
-- Name: forbid_ledger_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_ledger_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_models (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    model_identifier text NOT NULL,
    display_name text NOT NULL,
    context_limit integer,
    max_output_tokens integer,
    supports_streaming boolean DEFAULT true NOT NULL,
    supports_temperature boolean DEFAULT false NOT NULL,
    supports_reasoning_effort boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    availability_checked_at timestamp with time zone,
    sort_order integer DEFAULT 0 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ai_models_limits_positive CHECK (((COALESCE(context_limit, 1) > 0) AND (COALESCE(max_output_tokens, 1) > 0))),
    CONSTRAINT ck_ai_models_provider CHECK ((provider = ANY (ARRAY['openai'::text, 'anthropic'::text])))
);

--
-- Name: ai_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_usage (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    session_id uuid,
    request_id uuid NOT NULL,
    reservation_id uuid,
    model_id uuid,
    pricing_id uuid,
    provider text NOT NULL,
    model text NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cached_tokens integer DEFAULT 0 NOT NULL,
    cache_write_tokens integer DEFAULT 0 NOT NULL,
    reasoning_tokens integer DEFAULT 0 NOT NULL,
    estimated_cost numeric(18,8),
    provider_cost numeric(18,8),
    provider_currency text DEFAULT 'USD'::text NOT NULL,
    exchange_rate numeric(18,8),
    cost_eur numeric(18,8),
    wc_per_eur numeric(18,8),
    credits_reserved bigint DEFAULT 0 NOT NULL,
    credits_charged bigint DEFAULT 0 NOT NULL,
    cost_basis text DEFAULT 'none'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    streamed boolean DEFAULT false NOT NULL,
    provider_request_id text,
    stop_reason text,
    error_code text,
    error_message text,
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_ai_usage_cost_basis CHECK ((cost_basis = ANY (ARRAY['provider_usage'::text, 'estimated'::text, 'none'::text]))),
    CONSTRAINT ck_ai_usage_money_nonnegative CHECK (((COALESCE(estimated_cost, (0)::numeric) >= (0)::numeric) AND (COALESCE(provider_cost, (0)::numeric) >= (0)::numeric) AND (COALESCE(cost_eur, (0)::numeric) >= (0)::numeric) AND (COALESCE(exchange_rate, (1)::numeric) > (0)::numeric) AND (COALESCE(wc_per_eur, (1)::numeric) > (0)::numeric) AND (credits_reserved >= 0) AND (credits_charged >= 0) AND (COALESCE(duration_ms, 0) >= 0))),
    CONSTRAINT ck_ai_usage_provider CHECK ((provider = ANY (ARRAY['openai'::text, 'anthropic'::text]))),
    CONSTRAINT ck_ai_usage_status CHECK ((status = ANY (ARRAY['pending'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text, 'timeout'::text, 'unknown'::text]))),
    CONSTRAINT ck_ai_usage_tokens_nonnegative CHECK (((input_tokens >= 0) AND (output_tokens >= 0) AND (cached_tokens >= 0) AND (cache_write_tokens >= 0) AND (reasoning_tokens >= 0)))
);

--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    actor_user_id uuid,
    actor_type text NOT NULL,
    action text NOT NULL,
    entity_type text,
    entity_id text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    ip_address text,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_audit_log_actor_type CHECK ((actor_type = ANY (ARRAY['user'::text, 'admin'::text, 'device'::text, 'system'::text, 'anonymous'::text])))
);

--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.audit_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: auth_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    family_id uuid NOT NULL,
    token_hash text NOT NULL,
    rotated_from uuid,
    user_agent text,
    ip_address text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoked_reason text
);

--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    status text DEFAULT 'complete'::text NOT NULL,
    usage_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_chat_messages_role CHECK ((role = ANY (ARRAY['system'::text, 'user'::text, 'assistant'::text]))),
    CONSTRAINT ck_chat_messages_status CHECK ((status = ANY (ARRAY['complete'::text, 'partial'::text, 'error'::text])))
);

--
-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    title text DEFAULT 'New conversation'::text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    model_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_chat_sessions_provider CHECK ((provider = ANY (ARRAY['openai'::text, 'anthropic'::text])))
);

--
-- Name: daily_rewards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.daily_rewards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    steps_counted integer DEFAULT 0 NOT NULL,
    minutes_counted integer DEFAULT 0 NOT NULL,
    step_credits integer DEFAULT 0 NOT NULL,
    workout_credits integer DEFAULT 0 NOT NULL,
    entitled_credits integer DEFAULT 0 NOT NULL,
    credits_awarded integer DEFAULT 0 NOT NULL,
    rules_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_test_data boolean DEFAULT false NOT NULL,
    calculated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_daily_rewards_nonnegative CHECK (((steps_counted >= 0) AND (minutes_counted >= 0) AND (step_credits >= 0) AND (workout_credits >= 0) AND (entitled_credits >= 0) AND (credits_awarded >= 0)))
);

--
-- Name: device_pairing_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_pairing_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    code_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    device_token_id uuid
);

--
-- Name: device_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    platform text DEFAULT 'ios'::text NOT NULL,
    token_hash text NOT NULL,
    scopes text[] DEFAULT ARRAY['fitness:sync'::text] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);

--
-- Name: exchange_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_rates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    base_currency text NOT NULL,
    quote_currency text NOT NULL,
    rate numeric(18,8) NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    effective_until timestamp with time zone,
    source text DEFAULT 'manual'::text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_exchange_rates_currencies CHECK (((base_currency ~ '^[A-Z]{2,3}$'::text) AND (quote_currency ~ '^[A-Z]{2,3}$'::text) AND (base_currency <> quote_currency))),
    CONSTRAINT ck_exchange_rates_range CHECK (((effective_until IS NULL) OR (effective_until > effective_from))),
    CONSTRAINT ck_exchange_rates_rate_positive CHECK ((rate > (0)::numeric))
);

--
-- Name: fitness_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fitness_daily (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    source text NOT NULL,
    steps integer DEFAULT 0 NOT NULL,
    walking_distance_m numeric(12,2),
    running_distance_m numeric(12,2),
    cycling_distance_m numeric(12,2),
    active_minutes integer DEFAULT 0 NOT NULL,
    active_energy_kcal numeric(10,2),
    device_timezone text,
    is_test_data boolean DEFAULT false NOT NULL,
    last_batch_id uuid,
    synced_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_fitness_daily_nonnegative CHECK (((steps >= 0) AND (active_minutes >= 0) AND (COALESCE(walking_distance_m, (0)::numeric) >= (0)::numeric) AND (COALESCE(running_distance_m, (0)::numeric) >= (0)::numeric) AND (COALESCE(cycling_distance_m, (0)::numeric) >= (0)::numeric) AND (COALESCE(active_energy_kcal, (0)::numeric) >= (0)::numeric))),
    CONSTRAINT ck_fitness_daily_source CHECK ((source = ANY (ARRAY['apple_healthkit'::text, 'test'::text, 'manual'::text]))),
    CONSTRAINT ck_fitness_daily_test_flag CHECK ((is_test_data = (source = 'test'::text)))
);

--
-- Name: fitness_sync_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fitness_sync_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    device_token_id uuid,
    client_batch_id uuid NOT NULL,
    source text NOT NULL,
    is_test_data boolean DEFAULT false NOT NULL,
    payload_sha256 text NOT NULL,
    days_received integer DEFAULT 0 NOT NULL,
    workouts_received integer DEFAULT 0 NOT NULL,
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_fitness_sync_batches_counts CHECK (((days_received >= 0) AND (workouts_received >= 0))),
    CONSTRAINT ck_fitness_sync_batches_source CHECK ((source = ANY (ARRAY['apple_healthkit'::text, 'test'::text, 'manual'::text]))),
    CONSTRAINT ck_fitness_sync_batches_test_flag CHECK ((is_test_data = (source = 'test'::text)))
);

--
-- Name: model_pricing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_pricing (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_id uuid NOT NULL,
    input_price numeric(18,8) NOT NULL,
    output_price numeric(18,8) NOT NULL,
    cached_input_price numeric(18,8),
    cache_write_price numeric(18,8),
    cache_write_1h_price numeric(18,8),
    long_context_threshold integer,
    long_context_input_price numeric(18,8),
    long_context_output_price numeric(18,8),
    extra_prices jsonb DEFAULT '{}'::jsonb NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    effective_until timestamp with time zone,
    verified boolean DEFAULT false NOT NULL,
    verified_at timestamp with time zone,
    source_url text,
    notes text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_model_pricing_currency CHECK ((currency ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT ck_model_pricing_long_context CHECK (((long_context_threshold IS NULL) = ((long_context_input_price IS NULL) AND (long_context_output_price IS NULL)))),
    CONSTRAINT ck_model_pricing_nonnegative CHECK (((input_price >= (0)::numeric) AND (output_price >= (0)::numeric) AND (COALESCE(cached_input_price, (0)::numeric) >= (0)::numeric) AND (COALESCE(cache_write_price, (0)::numeric) >= (0)::numeric) AND (COALESCE(cache_write_1h_price, (0)::numeric) >= (0)::numeric) AND (COALESCE(long_context_input_price, (0)::numeric) >= (0)::numeric) AND (COALESCE(long_context_output_price, (0)::numeric) >= (0)::numeric) AND (COALESCE(long_context_threshold, 1) > 0))),
    CONSTRAINT ck_model_pricing_range CHECK (((effective_until IS NULL) OR (effective_until > effective_from))),
    CONSTRAINT ck_model_pricing_verified_at CHECK ((verified = (verified_at IS NOT NULL))),
    CONSTRAINT ck_model_pricing_verified_source CHECK (((NOT verified) OR (source_url IS NOT NULL)))
);

--
-- Name: reward_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reward_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    activity_type text NOT NULL,
    conversion_rate numeric(12,6),
    daily_limit integer,
    effective_from date NOT NULL,
    effective_until date,
    enabled boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reward_rules_activity_type CHECK ((activity_type = ANY (ARRAY['steps'::text, 'workout_minutes'::text, 'daily_total'::text]))),
    CONSTRAINT ck_reward_rules_cap_presence CHECK (((activity_type <> 'daily_total'::text) OR (daily_limit IS NOT NULL))),
    CONSTRAINT ck_reward_rules_nonnegative CHECK (((COALESCE(conversion_rate, (0)::numeric) >= (0)::numeric) AND (COALESCE(daily_limit, 0) >= 0))),
    CONSTRAINT ck_reward_rules_range CHECK (((effective_until IS NULL) OR (effective_until > effective_from))),
    CONSTRAINT ck_reward_rules_rate_presence CHECK (((activity_type = 'daily_total'::text) = (conversion_rate IS NULL)))
);

--
-- Name: spending_limits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spending_limits (
    user_id uuid NOT NULL,
    max_request_wc integer,
    max_daily_wc integer,
    max_monthly_wc integer,
    min_balance_wc integer DEFAULT 0 NOT NULL,
    alert_threshold_pct smallint DEFAULT 80 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_spending_limits_alert_pct CHECK (((alert_threshold_pct >= 1) AND (alert_threshold_pct <= 100))),
    CONSTRAINT ck_spending_limits_positive CHECK (((COALESCE(max_request_wc, 1) > 0) AND (COALESCE(max_daily_wc, 1) > 0) AND (COALESCE(max_monthly_wc, 1) > 0) AND (min_balance_wc >= 0)))
);

--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email public.citext NOT NULL,
    password_hash text NOT NULL,
    display_name text,
    role text DEFAULT 'user'::text NOT NULL,
    account_status text DEFAULT 'active'::text NOT NULL,
    timezone text DEFAULT 'Europe/Lisbon'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_users_account_status CHECK ((account_status = ANY (ARRAY['active'::text, 'suspended'::text, 'deleted'::text]))),
    CONSTRAINT ck_users_role CHECK ((role = ANY (ARRAY['user'::text, 'admin'::text])))
);

--
-- Name: wallet_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    available_balance bigint DEFAULT 0 NOT NULL,
    reserved_balance bigint DEFAULT 0 NOT NULL,
    lifetime_earned bigint DEFAULT 0 NOT NULL,
    lifetime_spent bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_accounts_available_nonnegative CHECK ((available_balance >= 0)),
    CONSTRAINT ck_wallet_accounts_lifetime_nonnegative CHECK (((lifetime_earned >= 0) AND (lifetime_spent >= 0))),
    CONSTRAINT ck_wallet_accounts_reserved_nonnegative CHECK ((reserved_balance >= 0))
);

--
-- Name: wallet_reservations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_reservations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    wallet_id uuid NOT NULL,
    request_id uuid NOT NULL,
    reserved_amount bigint NOT NULL,
    settled_amount bigint,
    released_amount bigint,
    status text DEFAULT 'active'::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    CONSTRAINT ck_wallet_reservations_amounts CHECK (((reserved_amount > 0) AND (COALESCE(settled_amount, (0)::bigint) >= 0) AND (COALESCE(released_amount, (0)::bigint) >= 0))),
    CONSTRAINT ck_wallet_reservations_closed CHECK (((status = 'active'::text) = (closed_at IS NULL))),
    CONSTRAINT ck_wallet_reservations_status CHECK ((status = ANY (ARRAY['active'::text, 'settled'::text, 'released'::text, 'expired'::text])))
);

--
-- Name: wallet_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_transactions (
    id bigint NOT NULL,
    user_id uuid NOT NULL,
    wallet_id uuid NOT NULL,
    transaction_type text NOT NULL,
    amount bigint NOT NULL,
    reserved_delta bigint DEFAULT 0 NOT NULL,
    balance_after bigint NOT NULL,
    reserved_after bigint NOT NULL,
    reference_type text,
    reference_id uuid,
    idempotency_key text,
    description text DEFAULT ''::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_transactions_balances_nonnegative CHECK (((balance_after >= 0) AND (reserved_after >= 0))),
    CONSTRAINT ck_wallet_transactions_shape CHECK (
CASE transaction_type
    WHEN 'ACTIVITY_REWARD'::text THEN ((amount > 0) AND (reserved_delta = 0))
    WHEN 'REWARD_REVERSAL'::text THEN ((amount < 0) AND (reserved_delta = 0))
    WHEN 'CREDIT_RESERVATION'::text THEN ((amount < 0) AND (reserved_delta = (- amount)))
    WHEN 'CREDIT_RELEASE'::text THEN ((amount > 0) AND (reserved_delta = (- amount)))
    WHEN 'AI_USAGE'::text THEN ((amount <= 0) AND (reserved_delta <= 0) AND ((amount + reserved_delta) < 0))
    ELSE ((amount <> 0) AND (reserved_delta = 0))
END),
    CONSTRAINT ck_wallet_transactions_type CHECK ((transaction_type = ANY (ARRAY['ACTIVITY_REWARD'::text, 'REWARD_REVERSAL'::text, 'AI_USAGE'::text, 'CREDIT_RESERVATION'::text, 'CREDIT_RELEASE'::text, 'MANUAL_ADJUSTMENT'::text, 'ADMIN_ADJUSTMENT'::text])))
);

--
-- Name: wallet_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.wallet_transactions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.wallet_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: workout_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workout_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    source text NOT NULL,
    external_id text NOT NULL,
    workout_type text NOT NULL,
    start_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone NOT NULL,
    duration_minutes numeric(8,2) NOT NULL,
    distance_m numeric(12,2),
    active_energy_kcal numeric(10,2),
    origin_bundle_id text,
    origin_device text,
    was_user_entered boolean DEFAULT false NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    is_test_data boolean DEFAULT false NOT NULL,
    excluded_from_rewards boolean DEFAULT false NOT NULL,
    exclusion_reason text,
    duplicate_of uuid,
    deleted_at timestamp with time zone,
    last_batch_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workout_events_interval CHECK ((end_time > start_time)),
    CONSTRAINT ck_workout_events_nonnegative CHECK (((duration_minutes >= (0)::numeric) AND (COALESCE(distance_m, (0)::numeric) >= (0)::numeric) AND (COALESCE(active_energy_kcal, (0)::numeric) >= (0)::numeric))),
    CONSTRAINT ck_workout_events_source CHECK ((source = ANY (ARRAY['apple_healthkit'::text, 'test'::text, 'manual'::text]))),
    CONSTRAINT ck_workout_events_test_flag CHECK ((is_test_data = (source = 'test'::text)))
);

--
-- Name: workout_type_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workout_type_rules (
    workout_type text NOT NULL,
    category text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workout_type_rules_category CHECK ((category = ANY (ARRAY['step_based'::text, 'non_step'::text, 'excluded'::text])))
);

--
-- Name: exchange_rates ex_exchange_rates_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT ex_exchange_rates_no_overlap EXCLUDE USING gist (base_currency WITH =, quote_currency WITH =, tstzrange(effective_from, effective_until, '[)'::text) WITH &&);

--
-- Name: model_pricing ex_model_pricing_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT ex_model_pricing_no_overlap EXCLUDE USING gist (model_id WITH =, tstzrange(effective_from, effective_until, '[)'::text) WITH &&);

--
-- Name: reward_rules ex_reward_rules_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_rules
    ADD CONSTRAINT ex_reward_rules_no_overlap EXCLUDE USING gist (activity_type WITH =, daterange(effective_from, effective_until, '[)'::text) WITH &&) WHERE (enabled);

--
-- Name: ai_models pk_ai_models; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT pk_ai_models PRIMARY KEY (id);

--
-- Name: ai_usage pk_ai_usage; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT pk_ai_usage PRIMARY KEY (id);

--
-- Name: audit_log pk_audit_log; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT pk_audit_log PRIMARY KEY (id);

--
-- Name: auth_sessions pk_auth_sessions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT pk_auth_sessions PRIMARY KEY (id);

--
-- Name: chat_messages pk_chat_messages; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT pk_chat_messages PRIMARY KEY (id);

--
-- Name: chat_sessions pk_chat_sessions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT pk_chat_sessions PRIMARY KEY (id);

--
-- Name: daily_rewards pk_daily_rewards; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daily_rewards
    ADD CONSTRAINT pk_daily_rewards PRIMARY KEY (id);

--
-- Name: device_pairing_codes pk_device_pairing_codes; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairing_codes
    ADD CONSTRAINT pk_device_pairing_codes PRIMARY KEY (id);

--
-- Name: device_tokens pk_device_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT pk_device_tokens PRIMARY KEY (id);

--
-- Name: exchange_rates pk_exchange_rates; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT pk_exchange_rates PRIMARY KEY (id);

--
-- Name: fitness_daily pk_fitness_daily; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_daily
    ADD CONSTRAINT pk_fitness_daily PRIMARY KEY (id);

--
-- Name: fitness_sync_batches pk_fitness_sync_batches; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_sync_batches
    ADD CONSTRAINT pk_fitness_sync_batches PRIMARY KEY (id);

--
-- Name: model_pricing pk_model_pricing; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT pk_model_pricing PRIMARY KEY (id);

--
-- Name: reward_rules pk_reward_rules; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_rules
    ADD CONSTRAINT pk_reward_rules PRIMARY KEY (id);

--
-- Name: spending_limits pk_spending_limits; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spending_limits
    ADD CONSTRAINT pk_spending_limits PRIMARY KEY (user_id);

--
-- Name: users pk_users; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

--
-- Name: wallet_accounts pk_wallet_accounts; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_accounts
    ADD CONSTRAINT pk_wallet_accounts PRIMARY KEY (id);

--
-- Name: wallet_reservations pk_wallet_reservations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_reservations
    ADD CONSTRAINT pk_wallet_reservations PRIMARY KEY (id);

--
-- Name: wallet_transactions pk_wallet_transactions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT pk_wallet_transactions PRIMARY KEY (id);

--
-- Name: workout_events pk_workout_events; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_events
    ADD CONSTRAINT pk_workout_events PRIMARY KEY (id);

--
-- Name: workout_type_rules pk_workout_type_rules; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_type_rules
    ADD CONSTRAINT pk_workout_type_rules PRIMARY KEY (workout_type);

--
-- Name: ai_models uq_ai_models_provider_identifier; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT uq_ai_models_provider_identifier UNIQUE (provider, model_identifier);

--
-- Name: ai_usage uq_ai_usage_request_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT uq_ai_usage_request_id UNIQUE (request_id);

--
-- Name: auth_sessions uq_auth_sessions_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT uq_auth_sessions_token_hash UNIQUE (token_hash);

--
-- Name: daily_rewards uq_daily_rewards_user_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daily_rewards
    ADD CONSTRAINT uq_daily_rewards_user_date UNIQUE (user_id, activity_date);

--
-- Name: device_pairing_codes uq_device_pairing_codes_code_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairing_codes
    ADD CONSTRAINT uq_device_pairing_codes_code_hash UNIQUE (code_hash);

--
-- Name: device_tokens uq_device_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT uq_device_tokens_token_hash UNIQUE (token_hash);

--
-- Name: fitness_daily uq_fitness_daily_user_date_source; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_daily
    ADD CONSTRAINT uq_fitness_daily_user_date_source UNIQUE (user_id, activity_date, source);

--
-- Name: fitness_sync_batches uq_fitness_sync_batches_user_batch; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_sync_batches
    ADD CONSTRAINT uq_fitness_sync_batches_user_batch UNIQUE (user_id, client_batch_id);

--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);

--
-- Name: wallet_accounts uq_wallet_accounts_user_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_accounts
    ADD CONSTRAINT uq_wallet_accounts_user_id UNIQUE (user_id);

--
-- Name: wallet_reservations uq_wallet_reservations_request_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_reservations
    ADD CONSTRAINT uq_wallet_reservations_request_id UNIQUE (request_id);

--
-- Name: workout_events uq_workout_events_user_source_external; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_events
    ADD CONSTRAINT uq_workout_events_user_source_external UNIQUE (user_id, source, external_id);

--
-- Name: ix_ai_usage_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_usage_session_id ON public.ai_usage USING btree (session_id);

--
-- Name: ix_ai_usage_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_usage_user_created ON public.ai_usage USING btree (user_id, created_at);

--
-- Name: ix_audit_log_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_created_at ON public.audit_log USING btree (created_at);

--
-- Name: ix_audit_log_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_entity ON public.audit_log USING btree (entity_type, entity_id);

--
-- Name: ix_auth_sessions_family_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_sessions_family_id ON public.auth_sessions USING btree (family_id);

--
-- Name: ix_auth_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_sessions_user_id ON public.auth_sessions USING btree (user_id);

--
-- Name: ix_chat_messages_session_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_session_created ON public.chat_messages USING btree (session_id, created_at);

--
-- Name: ix_chat_sessions_user_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_sessions_user_updated ON public.chat_sessions USING btree (user_id, updated_at);

--
-- Name: ix_device_pairing_codes_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_device_pairing_codes_user_id ON public.device_pairing_codes USING btree (user_id);

--
-- Name: ix_device_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_device_tokens_user_id ON public.device_tokens USING btree (user_id);

--
-- Name: ix_wallet_reservations_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_reservations_active ON public.wallet_reservations USING btree (expires_at) WHERE (status = 'active'::text);

--
-- Name: ix_wallet_reservations_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_reservations_user_created ON public.wallet_reservations USING btree (user_id, created_at);

--
-- Name: ix_wallet_transactions_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_transactions_reference ON public.wallet_transactions USING btree (reference_type, reference_id);

--
-- Name: ix_wallet_transactions_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_transactions_user_created ON public.wallet_transactions USING btree (user_id, created_at);

--
-- Name: ix_wallet_transactions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_transactions_user_id ON public.wallet_transactions USING btree (user_id, id);

--
-- Name: ix_workout_events_user_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workout_events_user_start ON public.workout_events USING btree (user_id, start_time);

--
-- Name: uq_wallet_transactions_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_wallet_transactions_idempotency ON public.wallet_transactions USING btree (user_id, idempotency_key) WHERE (idempotency_key IS NOT NULL);

--
-- Name: audit_log trg_audit_log_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_log_immutable BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.forbid_audit_mutation();

--
-- Name: audit_log trg_audit_log_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_log_no_truncate BEFORE TRUNCATE ON public.audit_log FOR EACH STATEMENT EXECUTE FUNCTION public.forbid_audit_mutation();

--
-- Name: wallet_transactions trg_wallet_transactions_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_wallet_transactions_immutable BEFORE DELETE OR UPDATE ON public.wallet_transactions FOR EACH ROW EXECUTE FUNCTION public.forbid_ledger_mutation();

--
-- Name: wallet_transactions trg_wallet_transactions_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_wallet_transactions_no_truncate BEFORE TRUNCATE ON public.wallet_transactions FOR EACH STATEMENT EXECUTE FUNCTION public.forbid_ledger_mutation();

--
-- Name: ai_usage fk_ai_usage_model_id_ai_models; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT fk_ai_usage_model_id_ai_models FOREIGN KEY (model_id) REFERENCES public.ai_models(id) ON DELETE RESTRICT;

--
-- Name: ai_usage fk_ai_usage_pricing_id_model_pricing; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT fk_ai_usage_pricing_id_model_pricing FOREIGN KEY (pricing_id) REFERENCES public.model_pricing(id) ON DELETE RESTRICT;

--
-- Name: ai_usage fk_ai_usage_reservation_id_wallet_reservations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT fk_ai_usage_reservation_id_wallet_reservations FOREIGN KEY (reservation_id) REFERENCES public.wallet_reservations(id) ON DELETE RESTRICT;

--
-- Name: ai_usage fk_ai_usage_session_id_chat_sessions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT fk_ai_usage_session_id_chat_sessions FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE SET NULL;

--
-- Name: ai_usage fk_ai_usage_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT fk_ai_usage_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: audit_log fk_audit_log_actor_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT fk_audit_log_actor_user_id_users FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: auth_sessions fk_auth_sessions_rotated_from_auth_sessions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT fk_auth_sessions_rotated_from_auth_sessions FOREIGN KEY (rotated_from) REFERENCES public.auth_sessions(id) ON DELETE SET NULL;

--
-- Name: auth_sessions fk_auth_sessions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT fk_auth_sessions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: chat_messages fk_chat_messages_session_id_chat_sessions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT fk_chat_messages_session_id_chat_sessions FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;

--
-- Name: chat_messages fk_chat_messages_usage_id_ai_usage; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT fk_chat_messages_usage_id_ai_usage FOREIGN KEY (usage_id) REFERENCES public.ai_usage(id) ON DELETE SET NULL;

--
-- Name: chat_sessions fk_chat_sessions_model_id_ai_models; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT fk_chat_sessions_model_id_ai_models FOREIGN KEY (model_id) REFERENCES public.ai_models(id) ON DELETE SET NULL;

--
-- Name: chat_sessions fk_chat_sessions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT fk_chat_sessions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: daily_rewards fk_daily_rewards_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daily_rewards
    ADD CONSTRAINT fk_daily_rewards_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: device_pairing_codes fk_device_pairing_codes_device_token_id_device_tokens; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairing_codes
    ADD CONSTRAINT fk_device_pairing_codes_device_token_id_device_tokens FOREIGN KEY (device_token_id) REFERENCES public.device_tokens(id) ON DELETE SET NULL;

--
-- Name: device_pairing_codes fk_device_pairing_codes_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairing_codes
    ADD CONSTRAINT fk_device_pairing_codes_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: device_tokens fk_device_tokens_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT fk_device_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: exchange_rates fk_exchange_rates_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT fk_exchange_rates_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: fitness_daily fk_fitness_daily_last_batch_id_fitness_sync_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_daily
    ADD CONSTRAINT fk_fitness_daily_last_batch_id_fitness_sync_batches FOREIGN KEY (last_batch_id) REFERENCES public.fitness_sync_batches(id) ON DELETE SET NULL;

--
-- Name: fitness_daily fk_fitness_daily_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_daily
    ADD CONSTRAINT fk_fitness_daily_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: fitness_sync_batches fk_fitness_sync_batches_device_token_id_device_tokens; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_sync_batches
    ADD CONSTRAINT fk_fitness_sync_batches_device_token_id_device_tokens FOREIGN KEY (device_token_id) REFERENCES public.device_tokens(id) ON DELETE SET NULL;

--
-- Name: fitness_sync_batches fk_fitness_sync_batches_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fitness_sync_batches
    ADD CONSTRAINT fk_fitness_sync_batches_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: model_pricing fk_model_pricing_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT fk_model_pricing_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: model_pricing fk_model_pricing_model_id_ai_models; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT fk_model_pricing_model_id_ai_models FOREIGN KEY (model_id) REFERENCES public.ai_models(id) ON DELETE RESTRICT;

--
-- Name: reward_rules fk_reward_rules_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_rules
    ADD CONSTRAINT fk_reward_rules_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: spending_limits fk_spending_limits_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spending_limits
    ADD CONSTRAINT fk_spending_limits_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: wallet_accounts fk_wallet_accounts_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_accounts
    ADD CONSTRAINT fk_wallet_accounts_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: wallet_reservations fk_wallet_reservations_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_reservations
    ADD CONSTRAINT fk_wallet_reservations_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: wallet_reservations fk_wallet_reservations_wallet_id_wallet_accounts; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_reservations
    ADD CONSTRAINT fk_wallet_reservations_wallet_id_wallet_accounts FOREIGN KEY (wallet_id) REFERENCES public.wallet_accounts(id) ON DELETE RESTRICT;

--
-- Name: wallet_transactions fk_wallet_transactions_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT fk_wallet_transactions_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: wallet_transactions fk_wallet_transactions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT fk_wallet_transactions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: wallet_transactions fk_wallet_transactions_wallet_id_wallet_accounts; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT fk_wallet_transactions_wallet_id_wallet_accounts FOREIGN KEY (wallet_id) REFERENCES public.wallet_accounts(id) ON DELETE RESTRICT;

--
-- Name: workout_events fk_workout_events_duplicate_of_workout_events; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_events
    ADD CONSTRAINT fk_workout_events_duplicate_of_workout_events FOREIGN KEY (duplicate_of) REFERENCES public.workout_events(id) ON DELETE SET NULL;

--
-- Name: workout_events fk_workout_events_last_batch_id_fitness_sync_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_events
    ADD CONSTRAINT fk_workout_events_last_batch_id_fitness_sync_batches FOREIGN KEY (last_batch_id) REFERENCES public.fitness_sync_batches(id) ON DELETE SET NULL;

--
-- Name: workout_events fk_workout_events_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workout_events
    ADD CONSTRAINT fk_workout_events_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- PostgreSQL database dump complete
--
