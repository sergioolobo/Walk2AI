"""Give chat messages a strictly increasing position.

A conversation is replayed to the model in order on every request. Ordering by
``created_at`` alone is not safe: a question and its answer can share a timestamp, and
the identifiers are random. A database-assigned sequence fixes the order for good.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_messages ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY")
    op.execute("DROP INDEX ix_chat_messages_session_created")
    op.execute("CREATE INDEX ix_chat_messages_session_seq ON chat_messages (session_id, seq)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_chat_messages_session_seq")
    op.execute(
        "CREATE INDEX ix_chat_messages_session_created ON chat_messages (session_id, created_at)"
    )
    op.execute("ALTER TABLE chat_messages DROP COLUMN seq")
