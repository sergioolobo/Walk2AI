"""Alembic environment.

The URL comes from the application settings unless the caller supplies one through
``config.attributes["sqlalchemy_url"]`` (the test-suite does, to target its own
database). Migrations run on a synchronous psycopg connection.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import URL, create_engine, pool

from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> URL | str:
    supplied: URL | str | None = config.attributes.get("sqlalchemy_url")
    if supplied is not None:
        return supplied
    from app.core.config import get_settings

    return get_settings().sqlalchemy_url


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    # Exclusion constraints are created by hand-written SQL and are backed by GiST
    # indexes that have no counterpart in the models; autogenerate must leave them be.
    return not (type_ == "index" and name is not None and name.startswith("ex_"))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
