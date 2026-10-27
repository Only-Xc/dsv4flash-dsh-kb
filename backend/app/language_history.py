"""Private working-history projection; public session events remain untouched."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import AuditRecord, Conversation, SessionEvent
from .language_contracts import (
    BRIDGE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    LanguageBridgeError,
)
from .language_prompts import PROMPT_VERSION


@dataclass(frozen=True)
class WorkingEvent:
    seq: int
    event_type: str
    event_json: dict[str, Any]
    created_at: datetime


async def load_language_records(
    db: AsyncSession, conversation: Conversation
) -> list[AuditRecord]:
    """Read only this owner's language audits using the existing audit retention policy."""
    result = await db.execute(
        select(AuditRecord)
        .where(
            AuditRecord.tenant_id == conversation.tenant_id,
            AuditRecord.user_id == conversation.user_id,
            AuditRecord.conversation_id == conversation.conversation_id,
            AuditRecord.record_type.in_(("language.input", "language.output")),
        )
        .order_by(AuditRecord.id)
    )
    return list(result.scalars().all())


def working_history(
    history: list[SessionEvent], records: list[AuditRecord]
) -> list[WorkingEvent]:
    """Project retained translations without changing ORM objects or tool evidence.

    Missing expired audits leave the original wording in context. They never
    create a fabricated translation or alter record-selection evidence.
    """
    inputs: dict[int, str] = {}
    outputs: dict[str, str] = {}
    for record in records:
        payload = record.payload
        if not isinstance(payload, dict):
            raise LanguageBridgeError("invalid_language_history")
        if payload.get("failure"):
            continue
        language = (
            payload.get("sourceLanguage")
            if record.record_type == "language.input"
            else payload.get("targetLanguage")
        )
        if (
            language in BRIDGE_LANGUAGES
            and payload.get("promptVersion") != PROMPT_VERSION
        ):
            # Older translations were not independently reviewed; keep original public wording.
            continue
        text = payload.get("workingText")
        if not isinstance(text, str):
            raise LanguageBridgeError("invalid_language_history")
        if record.record_type == "language.input":
            event_seq = payload.get("userEventSeq")
            if not isinstance(event_seq, int):
                raise LanguageBridgeError("invalid_language_history")
            inputs[event_seq] = text
        elif record.request_id:
            outputs[record.request_id] = text
    projected = []
    for event in history:
        payload = dict(event.event_json)
        if event.event_type == "assistant.message" and payload.get("streamStatus") == "failed":
            # A system failure receipt is not business evidence for the next turn.
            continue
        if event.event_type == "user.message" and event.seq in inputs:
            payload["content"] = inputs[event.seq]
        elif event.event_type == "assistant.message":
            request_id = str(payload.get("requestId") or "")
            if request_id in outputs:
                payload["content"] = outputs[request_id]
        projected.append(
            WorkingEvent(event.seq, event.event_type, payload, event.created_at)
        )
    return projected


def previous_language(history: list[SessionEvent]) -> str:
    """Use server-produced message metadata, never the Portal chrome locale."""
    for event in reversed(history):
        if event.event_type == "assistant.message":
            language = event.event_json.get("responseLanguage")
            if language in SUPPORTED_LANGUAGES:
                return language
    return "und"


def recent_language_context(history: list[SessionEvent]) -> list[dict[str, str]]:
    """Share only bounded conversational wording; no tool results or identity DTOs."""
    events = [
        event
        for event in history
        if event.event_type in {"user.message", "assistant.message"}
    ]
    return [
        {
            "role": "user" if event.event_type == "user.message" else "assistant",
            "content": str(event.event_json.get("content") or "")[:2000],
        }
        for event in events[-4:]
    ]
