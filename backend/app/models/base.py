"""Declarative base and shared column conventions.

IMPORTANT: the Alembic migrations are the schema. They carry the CHECK and EXCLUDE
constraints, the ledger-immutability triggers and the extensions. These models are the
*mapping* onto that schema — columns, keys, unique constraints and indexes — and the
test-suite proves the two agree. Never build a database with ``metadata.create_all()``;
it would lack the constraints that protect the wallet.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, MetaData, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this as a class-level mapping
        str: Text,
        int: Integer,
        bool: Boolean,
        uuid.UUID: Uuid,
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


# Explicit alias for credit amounts: Walk Credits are always 64-bit integers.
Credits = BigInteger
