"""Guarantees enforced by the database itself, exercised with raw SQL.

These hold even if application code is wrong: they are the reason a bug in a service
cannot corrupt the ledger. Every statement runs with the application's own database
role, i.e. with exactly the privileges the running backend has.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import ActorType
from app.services.audit import record_audit
from app.services.wallet import WalletService
from tests.support import MutableClock, create_user_with_wallet

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


async def execute(session_factory: Sessions, sql: str, **params: Any) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text(sql), params)


async def scalar(session_factory: Sessions, sql: str, **params: Any) -> Any:
    async with session_factory() as session:
        return await session.scalar(text(sql), params)


# ----------------------------------------------------------------- the ledger


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE wallet_transactions SET amount = 1000000",
        "UPDATE wallet_transactions SET description = 'edited'",
        "UPDATE wallet_transactions SET balance_after = balance_after + 1",
        "DELETE FROM wallet_transactions",
        "TRUNCATE wallet_transactions",
        "TRUNCATE wallet_transactions CASCADE",
    ],
)
async def test_ledger_rows_cannot_be_changed_or_removed(
    session_factory: Sessions, clock: MutableClock, statement: str
) -> None:
    await create_user_with_wallet(session_factory, clock, balance=500)

    with pytest.raises(DBAPIError, match="wallet_transactions is append-only"):
        await execute(session_factory, statement)

    assert await scalar(session_factory, "SELECT count(*) FROM wallet_transactions") == 1
    assert await scalar(session_factory, "SELECT amount FROM wallet_transactions") == 500


@pytest.mark.parametrize(
    ("column", "value"),
    [("available_balance", -1), ("reserved_balance", -1), ("lifetime_spent", -5)],
)
async def test_wallet_balances_cannot_be_negative(
    session_factory: Sessions, clock: MutableClock, column: str, value: int
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=100)
    with pytest.raises(IntegrityError, match="ck_wallet_accounts_"):
        await execute(
            session_factory,
            f"UPDATE wallet_accounts SET {column} = :v WHERE user_id = :u",
            v=value,
            u=user,
        )


@pytest.mark.parametrize(
    ("kind", "amount", "reserved_delta"),
    [
        ("ACTIVITY_REWARD", -10, 0),  # a reward cannot take credits away
        ("ACTIVITY_REWARD", 0, 0),
        ("ACTIVITY_REWARD", 10, 5),  # a reward never touches the reserved balance
        ("REWARD_REVERSAL", 10, 0),  # a reversal cannot add credits
        ("CREDIT_RESERVATION", -10, 9),  # what leaves available must arrive in reserved
        ("CREDIT_RESERVATION", 10, -10),
        ("CREDIT_RELEASE", 10, -9),
        ("CREDIT_RELEASE", -10, 10),
        ("AI_USAGE", 10, -10),  # usage can never increase a balance
        ("AI_USAGE", 0, 0),
        ("AI_USAGE", -5, 5),
        ("ADMIN_ADJUSTMENT", 0, 0),
        ("ADMIN_ADJUSTMENT", 10, 10),
        ("MADE_UP_TYPE", 10, 0),
    ],
)
async def test_malformed_ledger_rows_are_rejected(
    session_factory: Sessions, clock: MutableClock, kind: str, amount: int, reserved_delta: int
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    wallet_id = await scalar(
        session_factory, "SELECT id FROM wallet_accounts WHERE user_id = :u", u=user
    )
    with pytest.raises(IntegrityError, match="ck_wallet_transactions_"):
        await execute(
            session_factory,
            "INSERT INTO wallet_transactions "
            "(user_id, wallet_id, transaction_type, amount, reserved_delta, "
            " balance_after, reserved_after) "
            "VALUES (:u, :w, :k, :a, :r, 0, 0)",
            u=user,
            w=wallet_id,
            k=kind,
            a=amount,
            r=reserved_delta,
        )


async def test_idempotency_key_is_unique_per_user(
    session_factory: Sessions, clock: MutableClock
) -> None:
    first = await create_user_with_wallet(session_factory, clock)
    second = await create_user_with_wallet(session_factory, clock)
    insert = (
        "INSERT INTO wallet_transactions "
        "(user_id, wallet_id, transaction_type, amount, balance_after, reserved_after, "
        " idempotency_key) "
        "SELECT user_id, id, 'ACTIVITY_REWARD', 10, 10, 0, 'same-key' "
        "FROM wallet_accounts WHERE user_id = :u"
    )
    await execute(session_factory, insert, u=first)
    await execute(session_factory, insert, u=second)  # another user may use the same key
    with pytest.raises(IntegrityError, match="uq_wallet_transactions_idempotency"):
        await execute(session_factory, insert, u=first)


async def test_a_user_with_a_ledger_cannot_be_deleted(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Financial history outlives everything: accounts are disabled, never erased."""
    user = await create_user_with_wallet(session_factory, clock, balance=100)
    with pytest.raises(IntegrityError, match="fk_wallet_"):
        await execute(session_factory, "DELETE FROM users WHERE id = :u", u=user)


async def test_reservation_state_must_be_coherent(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=500)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        from datetime import timedelta

        await WalletService(session, clock).reserve(
            user, 100, request_id=request_id, ttl=timedelta(minutes=5)
        )

    with pytest.raises(IntegrityError, match="ck_wallet_reservations_closed"):
        await execute(
            session_factory,
            "UPDATE wallet_reservations SET status = 'settled' WHERE request_id = :r",
            r=request_id,
        )
    with pytest.raises(IntegrityError, match="ck_wallet_reservations_status"):
        await execute(
            session_factory,
            "UPDATE wallet_reservations SET status = 'vanished', closed_at = now() "
            "WHERE request_id = :r",
            r=request_id,
        )
    with pytest.raises(IntegrityError, match="ck_wallet_reservations_amounts"):
        await execute(
            session_factory,
            "UPDATE wallet_reservations SET reserved_amount = 0 WHERE request_id = :r",
            r=request_id,
        )


# ------------------------------------------------------------------ the audit log


async def test_audit_log_is_append_only(session_factory: Sessions, clock: MutableClock) -> None:
    async with session_factory() as session, session.begin():
        record_audit(session, clock, action="test.event", actor_type=ActorType.SYSTEM)

    for statement in (
        "UPDATE audit_log SET action = 'rewritten'",
        "UPDATE audit_log SET details = '{\"forged\": true}'::jsonb",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(DBAPIError, match="audit_log is append-only"):
            await execute(session_factory, statement)
    assert await scalar(session_factory, "SELECT action FROM audit_log") == "test.event"


async def test_deleting_a_user_keeps_their_audit_trail(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """The one permitted change: a deleted actor is detached, the record remains."""
    user_id = uuid.uuid4()
    await execute(
        session_factory,
        "INSERT INTO users (id, email, password_hash) VALUES (:id, 'gone@example.com', 'x')",
        id=user_id,
    )
    async with session_factory() as session, session.begin():
        record_audit(
            session,
            clock,
            action="auth.login",
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            details={"note": "kept"},
        )

    await execute(session_factory, "DELETE FROM users WHERE id = :id", id=user_id)

    async with session_factory() as session:
        row = (
            await session.execute(text("SELECT actor_user_id, action, details FROM audit_log"))
        ).one()
    assert row == (None, "auth.login", {"note": "kept"})


# ------------------------------------------------------- versioned configuration


async def test_reward_rules_cannot_overlap(session_factory: Sessions) -> None:
    insert = (
        "INSERT INTO reward_rules (activity_type, conversion_rate, effective_from, "
        "effective_until) VALUES ('steps', :rate, :start, :end)"
    )
    from datetime import date

    await execute(session_factory, insert, rate=0.1, start=date(2026, 1, 1), end=None)

    with pytest.raises(IntegrityError, match="ex_reward_rules_no_overlap"):
        await execute(session_factory, insert, rate=0.2, start=date(2026, 6, 1), end=None)

    # Closing the current rule and succeeding it — what the admin API does — works.
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE reward_rules SET effective_until = :d WHERE effective_until IS NULL"),
            {"d": date(2026, 6, 1)},
        )
        await session.execute(text(insert), {"rate": 0.2, "start": date(2026, 6, 1), "end": None})

    lookup = (
        "SELECT conversion_rate FROM reward_rules WHERE activity_type = 'steps' AND enabled "
        "AND daterange(effective_from, effective_until, '[)') @> CAST(:day AS date)"
    )
    assert float(await scalar(session_factory, lookup, day=date(2026, 5, 31))) == 0.1
    assert float(await scalar(session_factory, lookup, day=date(2026, 6, 1))) == 0.2


async def test_reward_rule_shape_is_enforced(session_factory: Sessions) -> None:
    with pytest.raises(IntegrityError, match="ck_reward_rules_rate_presence"):
        await execute(
            session_factory,
            "INSERT INTO reward_rules (activity_type, daily_limit, effective_from) "
            "VALUES ('steps', 100, '2026-01-01')",
        )
    with pytest.raises(IntegrityError, match="ck_reward_rules_cap_presence"):
        await execute(
            session_factory,
            "INSERT INTO reward_rules (activity_type, effective_from) "
            "VALUES ('daily_total', '2026-01-01')",
        )
    with pytest.raises(IntegrityError, match="ck_reward_rules_range"):
        await execute(
            session_factory,
            "INSERT INTO reward_rules (activity_type, conversion_rate, effective_from, "
            "effective_until) VALUES ('steps', 0.1, '2026-06-01', '2026-01-01')",
        )
    with pytest.raises(IntegrityError, match="ck_reward_rules_nonnegative"):
        await execute(
            session_factory,
            "INSERT INTO reward_rules (activity_type, conversion_rate, effective_from) "
            "VALUES ('steps', -0.1, '2026-01-01')",
        )


async def test_exchange_rates_cannot_overlap_and_must_be_positive(
    session_factory: Sessions,
) -> None:
    insert = (
        "INSERT INTO exchange_rates (base_currency, quote_currency, rate, effective_from) "
        "VALUES (:base, :quote, :rate, '2026-01-01T00:00:00Z')"
    )
    await execute(session_factory, insert, base="EUR", quote="WC", rate=10000)
    await execute(session_factory, insert, base="USD", quote="EUR", rate=0.9)  # another pair
    with pytest.raises(IntegrityError, match="ex_exchange_rates_no_overlap"):
        await execute(session_factory, insert, base="EUR", quote="WC", rate=20000)
    with pytest.raises(IntegrityError, match="ck_exchange_rates_rate_positive"):
        await execute(session_factory, insert, base="GBP", quote="EUR", rate=0)
    with pytest.raises(IntegrityError, match="ck_exchange_rates_currencies"):
        await execute(session_factory, insert, base="EUR", quote="EUR", rate=1)


async def test_model_prices_are_versioned_and_a_verified_price_cites_its_source(
    session_factory: Sessions,
) -> None:
    model_id = uuid.uuid4()
    await execute(
        session_factory,
        "INSERT INTO ai_models (id, provider, model_identifier, display_name) "
        "VALUES (:id, 'anthropic', 'example-model', 'Example')",
        id=model_id,
    )
    assert (
        await scalar(session_factory, "SELECT enabled FROM ai_models WHERE id = :id", id=model_id)
        is False
    )  # a new model is unusable until verified and enabled

    insert = (
        "INSERT INTO model_pricing (model_id, input_price, output_price, effective_from, "
        "verified, verified_at, source_url) "
        "VALUES (:m, 1, 5, :start, :verified, :verified_at, :source)"
    )
    from datetime import UTC, datetime

    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="ck_model_pricing_verified_source"):
        await execute(
            session_factory, insert, m=model_id, start=start, verified=True,
            verified_at=start, source=None,
        )  # fmt: skip
    with pytest.raises(IntegrityError, match="ck_model_pricing_verified_at"):
        await execute(
            session_factory, insert, m=model_id, start=start, verified=True,
            verified_at=None, source="https://example.com/pricing",
        )  # fmt: skip
    await execute(
        session_factory, insert, m=model_id, start=start, verified=False,
        verified_at=None, source=None,
    )  # fmt: skip
    with pytest.raises(IntegrityError, match="ex_model_pricing_no_overlap"):
        await execute(
            session_factory, insert, m=model_id, start=datetime(2026, 6, 1, tzinfo=UTC),
            verified=False, verified_at=None, source=None,
        )  # fmt: skip
    with pytest.raises(IntegrityError, match="ck_model_pricing_nonnegative"):
        await execute(
            session_factory,
            "INSERT INTO model_pricing (model_id, input_price, output_price, effective_from) "
            "VALUES (:m, -1, 5, '2020-01-01T00:00:00Z')",
            m=model_id,
        )


# ----------------------------------------------------------------- fitness data


async def test_test_fitness_data_is_always_labelled(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Rows from the test source cannot exist without the test flag, and vice versa."""
    user = await create_user_with_wallet(session_factory, clock)
    insert = (
        "INSERT INTO fitness_daily (user_id, activity_date, source, steps, is_test_data) "
        "VALUES (:u, '2026-11-12', :source, 8000, :flag)"
    )
    with pytest.raises(IntegrityError, match="ck_fitness_daily_test_flag"):
        await execute(session_factory, insert, u=user, source="test", flag=False)
    with pytest.raises(IntegrityError, match="ck_fitness_daily_test_flag"):
        await execute(session_factory, insert, u=user, source="apple_healthkit", flag=True)
    with pytest.raises(IntegrityError, match="ck_fitness_daily_source"):
        await execute(session_factory, insert, u=user, source="made_up", flag=False)
    await execute(session_factory, insert, u=user, source="apple_healthkit", flag=False)


async def test_a_day_and_a_workout_exist_only_once(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    day = (
        "INSERT INTO fitness_daily (user_id, activity_date, source, steps) "
        "VALUES (:u, '2026-11-12', 'apple_healthkit', 8000)"
    )
    await execute(session_factory, day, u=user)
    with pytest.raises(IntegrityError, match="uq_fitness_daily_user_date_source"):
        await execute(session_factory, day, u=user)

    workout = (
        "INSERT INTO workout_events (user_id, source, external_id, workout_type, start_time, "
        "end_time, duration_minutes) VALUES (:u, 'apple_healthkit', 'HK-1', 'cycling', "
        ":start, :end, 30)"
    )
    from datetime import UTC, datetime

    start = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    end = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
    await execute(session_factory, workout, u=user, start=start, end=end)
    with pytest.raises(IntegrityError, match="uq_workout_events_user_source_external"):
        await execute(session_factory, workout, u=user, start=start, end=end)
    with pytest.raises(IntegrityError, match="ck_workout_events_interval"):
        await execute(
            session_factory,
            workout.replace("HK-1", "HK-2"),
            u=user,
            start=end,
            end=start,
        )

    reward = "INSERT INTO daily_rewards (user_id, activity_date) VALUES (:u, '2026-11-12')"
    await execute(session_factory, reward, u=user)
    with pytest.raises(IntegrityError, match="uq_daily_rewards_user_date"):
        await execute(session_factory, reward, u=user)


# ---------------------------------------------------------------------- accounts


async def test_email_is_unique_regardless_of_case(session_factory: Sessions) -> None:
    insert = "INSERT INTO users (email, password_hash) VALUES (:email, 'x')"
    await execute(session_factory, insert, email="Owner@Example.com")
    with pytest.raises(IntegrityError, match="uq_users_email"):
        await execute(session_factory, insert, email="owner@example.COM")


async def test_new_accounts_default_to_the_lisbon_timezone(session_factory: Sessions) -> None:
    await execute(
        session_factory, "INSERT INTO users (email, password_hash) VALUES ('a@example.com', 'x')"
    )
    assert await scalar(session_factory, "SELECT timezone FROM users") == "Europe/Lisbon"
    assert await scalar(session_factory, "SELECT role FROM users") == "user"
