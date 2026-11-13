"""Migrations, model/schema agreement and seed data."""

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import expected_schema_revision
from app.models import Base
from app.seeds import seed_initial_configuration
from tests.conftest import alembic_config

Sessions = async_sessionmaker[AsyncSession]

EXPECTED_TABLES = {
    "ai_models",
    "ai_usage",
    "audit_log",
    "auth_sessions",
    "chat_messages",
    "chat_sessions",
    "daily_rewards",
    "device_pairing_codes",
    "device_tokens",
    "exchange_rates",
    "fitness_daily",
    "fitness_sync_batches",
    "model_pricing",
    "reward_rules",
    "spending_limits",
    "users",
    "wallet_accounts",
    "wallet_reservations",
    "wallet_transactions",
    "workout_events",
    "workout_type_rules",
}


def _ignore_exclusion_indexes(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    return not (type_ == "index" and name is not None and name.startswith("ex_"))


def test_models_describe_exactly_the_migrated_schema(
    migrated_database: None, sync_engine: sa.Engine
) -> None:
    """No drift: the ORM mapping and the migrations agree on every table, column,
    type, key, unique constraint and index."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    with sync_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "include_object": _ignore_exclusion_indexes}
        )
        differences = compare_metadata(context, Base.metadata)
    assert differences == []


def test_database_is_at_the_revision_the_code_expects(
    migrated_database: None, sync_engine: sa.Engine
) -> None:
    with sync_engine.connect() as connection:
        applied = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert applied == expected_schema_revision() == "0001"


def test_integrity_objects_exist(migrated_database: None, sync_engine: sa.Engine) -> None:
    """The parts autogeneration cannot see are really there."""
    with sync_engine.connect() as connection:
        triggers = set(
            connection.execute(
                sa.text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname")
            ).scalars()
        )
        exclusions = set(
            connection.execute(
                sa.text("SELECT conname FROM pg_constraint WHERE contype = 'x'")
            ).scalars()
        )
        checks = connection.execute(
            sa.text("SELECT count(*) FROM pg_constraint WHERE contype = 'c'")
        ).scalar()
        float_columns = (
            connection.execute(
                sa.text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type IN ('real', 'double precision')"
                )
            )
            .scalars()
            .all()
        )
        naive_timestamps = (
            connection.execute(
                sa.text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone'"
                )
            )
            .scalars()
            .all()
        )

    assert triggers == {
        "trg_audit_log_immutable",
        "trg_audit_log_no_truncate",
        "trg_wallet_transactions_immutable",
        "trg_wallet_transactions_no_truncate",
    }
    assert exclusions == {
        "ex_exchange_rates_no_overlap",
        "ex_model_pricing_no_overlap",
        "ex_reward_rules_no_overlap",
    }
    assert checks is not None and checks >= 50
    assert float_columns == []  # no floating point anywhere in the schema
    assert naive_timestamps == []  # every timestamp is timezone-aware (stored in UTC)


def test_migrations_apply_revert_and_reapply_on_an_empty_database(database_url: sa.URL) -> None:
    """upgrade -> downgrade -> upgrade on a scratch database, leaving nothing behind."""
    scratch_name = f"walk2ai_migration_{uuid.uuid4().hex[:10]}_test"
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    scratch = sa.create_engine(database_url.set(database=scratch_name))
    tables_sql = sa.text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{scratch_name}"'))
        config = alembic_config(database_url.set(database=scratch_name))

        command.upgrade(config, "head")
        with scratch.connect() as connection:
            assert set(connection.execute(tables_sql).scalars()) == EXPECTED_TABLES | {
                "alembic_version"
            }

        command.downgrade(config, "base")
        with scratch.connect() as connection:
            assert set(connection.execute(tables_sql).scalars()) == {"alembic_version"}
            leftover = connection.execute(
                sa.text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'forbid_%_mutation'")
            ).scalar()
            assert leftover == 0

        command.upgrade(config, "head")
        with scratch.connect() as connection:
            assert len(set(connection.execute(tables_sql).scalars())) == len(EXPECTED_TABLES) + 1
    finally:
        scratch.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.anyio
async def test_seed_creates_the_initial_configuration_once(session_factory: Sessions) -> None:
    async with session_factory() as session, session.begin():
        first = await seed_initial_configuration(session)
    async with session_factory() as session, session.begin():
        second = await seed_initial_configuration(session)

    assert (first.reward_rules_created, first.exchange_rates_created) == (3, 1)
    assert first.workout_types_created >= 15
    assert (
        second.reward_rules_created,
        second.workout_types_created,
        second.exchange_rates_created,
    ) == (0, 0, 0)

    async with session_factory() as session:
        rules = {
            row.activity_type: (row.conversion_rate, row.daily_limit)
            for row in (
                await session.execute(
                    sa.text("SELECT activity_type, conversion_rate, daily_limit FROM reward_rules")
                )
            ).all()
        }
        wc_per_eur = await session.scalar(
            sa.text(
                "SELECT rate FROM exchange_rates "
                "WHERE base_currency = 'EUR' AND quote_currency = 'WC'"
            )
        )
        categories: dict[str, str] = dict(
            (
                await session.execute(
                    sa.text("SELECT workout_type, category FROM workout_type_rules")
                )
            ).all()
        )

    # 1,000 steps = 100 WC; 30 minutes = 150 WC; 1,500 WC daily cap; 1,000 WC = EUR 0.10.
    assert rules == {
        "steps": (Decimal("0.100000"), None),
        "workout_minutes": (Decimal("5.000000"), None),
        "daily_total": (None, 1500),
    }
    assert wc_per_eur == Decimal("10000")
    # Walking and running are paid through steps only: never as workout minutes too.
    assert categories["walking"] == categories["running"] == "step_based"
    assert categories["traditional_strength_training"] == categories["cycling"] == "non_step"


@pytest.mark.anyio
async def test_seed_does_not_overwrite_changed_configuration(session_factory: Sessions) -> None:
    async with session_factory() as session, session.begin():
        await seed_initial_configuration(session)
        await session.execute(
            sa.text(
                "UPDATE workout_type_rules SET category = 'excluded' WHERE workout_type = 'yoga'"
            )
        )
        await session.execute(
            sa.text(
                "UPDATE reward_rules SET daily_limit = 2000 WHERE activity_type = 'daily_total'"
            )
        )
    async with session_factory() as session, session.begin():
        await seed_initial_configuration(session)

    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.text("SELECT category FROM workout_type_rules WHERE workout_type = 'yoga'")
            )
            == "excluded"
        )
        assert (
            await session.scalar(
                sa.text("SELECT daily_limit FROM reward_rules WHERE activity_type = 'daily_total'")
            )
            == 2000
        )
