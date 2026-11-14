"""Turn a schema-only pg_dump (stdin) into docs/database/schema.sql (stdout).

Used by ``make schema-doc``. It strips the parts of a dump that change on every run
(version banners and pg_dump's random \\restrict tokens) so the file only changes when
the schema does, and adds a header saying where the file comes from.

Standard library only: runs with any Python 3.
"""

import re
import sys

HEADER = """\
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

"""

_VOLATILE = re.compile(r"^(\\(un)?restrict\b|-- Dumped )")


def render(dump: str) -> str:
    lines = [line for line in dump.splitlines() if not _VOLATILE.match(line)]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return HEADER + body + "\n"


if __name__ == "__main__":
    sys.stdout.write(render(sys.stdin.read()))
