from __future__ import annotations

"""Domain: agent event and file-edit event queries."""

import json
import logging

from sqlalchemy import select

from agentception.db.engine import get_session
from agentception.db.models import ACAgentEvent
from agentception.models import FileEditEvent

from agentception.db.queries.types import (
    AgentEventRow,
)

logger = logging.getLogger(__name__)

async def get_all_events_tail(
    run_id: str,
    after_id: int = 0,
) -> list[AgentEventRow]:
    """Return all agent events for *run_id* with ``id > after_id``, ordered by id ASC.

    Includes every event_type (step_start, done, file_edit, activity, etc.).
    Used by the inspector SSE stream to push events in chronological order.
    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentEvent)
                .where(
                    ACAgentEvent.agent_run_id == run_id,
                    ACAgentEvent.id > after_id,
                )
                .order_by(ACAgentEvent.id)
            )
            rows = result.scalars().all()

        return [
            AgentEventRow(
                id=row.id,
                event_type=row.event_type,
                payload=row.payload or "{}",
                recorded_at=row.recorded_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_all_events_tail DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_events_tail(
    run_id: str,
    after_id: int = 0,
) -> list[AgentEventRow]:
    """Return MCP-reported events for *run_id* with ``id > after_id``.

    Alias for get_all_events_tail. Used by the inspector SSE stream.
    Falls back to ``[]`` on DB error.
    """
    return await get_all_events_tail(run_id, after_id)


async def get_file_edit_events(run_id: str) -> list[FileEditEvent]:
    """Return all file-edit events for *run_id* from ``agent_events``.

    Queries ``ACAgentEvent`` rows where ``agent_run_id = run_id`` AND
    ``event_type = 'file_edit'``, then deserializes each row's ``payload``
    JSON field as a :class:`~agentception.models.FileEditEvent`.  Rows whose
    payload cannot be deserialized are silently skipped.  Falls back to ``[]``
    on DB error (non-fatal).
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentEvent)
                .where(
                    ACAgentEvent.agent_run_id == run_id,
                    ACAgentEvent.event_type == "file_edit",
                )
                .order_by(ACAgentEvent.id)
            )
            rows = result.scalars().all()

        events: list[FileEditEvent] = []
        for row in rows:
            try:
                payload = json.loads(row.payload or "{}")
                events.append(FileEditEvent(**payload))
            except Exception as parse_exc:
                logger.warning(
                    "⚠️  get_file_edit_events: skipping unparseable payload id=%d: %s",
                    row.id,
                    parse_exc,
                )
        return events
    except Exception as exc:
        logger.warning("⚠️  get_file_edit_events DB query failed (non-fatal): %s", exc)
        return []

