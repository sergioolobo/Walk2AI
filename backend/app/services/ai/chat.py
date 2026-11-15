"""Conversations: how they are stored, listed and replayed to a model."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.ai import AIUsage, ChatMessage, ChatSession
from app.services.ai.providers import Turn, TurnRole

COMPLETE = "complete"
PARTIAL = "partial"
ERROR = "error"
TITLE_LENGTH = 60


def title_from(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


async def owned_session(
    session: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatSession:
    chat = await session.scalar(
        select(ChatSession).where(ChatSession.id == chat_id, ChatSession.user_id == user_id)
    )
    if chat is None:
        raise NotFound("Conversation not found.")
    return chat


async def history(session: AsyncSession, chat_id: uuid.UUID, limit: int) -> list[Turn]:
    """The last ``limit`` complete messages, ready to replay.

    Only complete messages are replayed: a failed question, or an answer that was cut
    off, is shown to the user but not sent back to the model. Consecutive messages of
    the same role are merged and a leading answer is dropped, so the model always sees a
    well-formed exchange that starts with the user.
    """
    if limit <= 0:
        return []
    rows = (
        await session.execute(
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.session_id == chat_id, ChatMessage.status == COMPLETE)
            .order_by(ChatMessage.seq.desc())
            .limit(limit)
        )
    ).all()
    turns: list[Turn] = []
    for role, content in reversed(rows):
        turn_role = TurnRole(role)
        if not turns and turn_role is TurnRole.ASSISTANT:
            continue
        if turns and turns[-1].role is turn_role:
            turns[-1] = Turn(turn_role, f"{turns[-1].content}\n\n{content}")
        else:
            turns.append(Turn(turn_role, content))
    return turns


def with_new_message(turns: list[Turn], message: str) -> tuple[Turn, ...]:
    if turns and turns[-1].role is TurnRole.USER:
        return (*turns[:-1], Turn(TurnRole.USER, f"{turns[-1].content}\n\n{message}"))
    return (*turns, Turn(TurnRole.USER, message))


@dataclass(frozen=True)
class SessionSummary:
    chat: ChatSession
    messages: int
    credits_charged: int


async def list_sessions(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> list[SessionSummary]:
    message_count = (
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.session_id == ChatSession.id)
        .scalar_subquery()
    )
    charged = (
        select(func.coalesce(func.sum(AIUsage.credits_charged), 0))
        .where(AIUsage.session_id == ChatSession.id)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(ChatSession, message_count, charged)
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [SessionSummary(chat=c, messages=int(m), credits_charged=int(w)) for c, m, w in rows]


async def messages_with_usage(
    session: AsyncSession, chat_id: uuid.UUID
) -> Sequence[tuple[ChatMessage, AIUsage | None]]:
    rows = (
        await session.execute(
            select(ChatMessage, AIUsage)
            .outerjoin(AIUsage, AIUsage.id == ChatMessage.usage_id)
            .where(ChatMessage.session_id == chat_id)
            .order_by(ChatMessage.seq)
        )
    ).all()
    return [(message, usage) for message, usage in rows]
