"""Audit log writer.

An audit row is added to the caller's transaction, so it commits or rolls back with the
action it describes. ``details`` must never contain passwords, tokens, keys or message
content — only identifiers and before/after values of non-secret settings.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.models.audit import AuditLog
from app.models.enums import ActorType

_MAX_USER_AGENT = 300


@dataclass(frozen=True)
class RequestMeta:
    """Where a request came from, for the audit trail and session records."""

    ip_address: str | None = None
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if self.user_agent is not None and len(self.user_agent) > _MAX_USER_AGENT:
            object.__setattr__(self, "user_agent", self.user_agent[:_MAX_USER_AGENT])


def record_audit(
    session: AsyncSession,
    clock: Clock,
    *,
    action: str,
    actor_type: ActorType,
    actor_user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | uuid.UUID | None = None,
    details: Mapping[str, Any] | None = None,
    meta: RequestMeta | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        actor_type=actor_type.value,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=dict(details or {}),
        ip_address=meta.ip_address if meta else None,
        user_agent=meta.user_agent if meta else None,
        created_at=clock.now(),
    )
    session.add(entry)
    return entry
