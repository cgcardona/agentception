from __future__ import annotations

"""Domain: agent message and thought stream queries."""

import logging

from sqlalchemy import select

from agentception.db.engine import get_session
from agentception.db.models import ACAgentMessage

from agentception.db.queries.types import (
    AgentThoughtRow,
)

logger = logging.getLogger(__name__)

async def get_agent_thoughts_tail(
    run_id: str,
    after_seq: int = -1,
    roles: tuple[str, ...] = ("thinking", "assistant", "tool_call", "tool_result"),
) -> list[AgentThoughtRow]:
    """Return transcript messages for *run_id* with ``sequence_index > after_seq``.

    Defaults to thinking + assistant messages — the raw chain-of-thought stream
    stored by the agent loop (and optionally ingested from external sources).
    Falls back to ``[]`` on error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentMessage)
                .where(
                    ACAgentMessage.agent_run_id == run_id,
                    ACAgentMessage.sequence_index > after_seq,
                    ACAgentMessage.role.in_(list(roles)),
                )
                .order_by(ACAgentMessage.sequence_index)
                .limit(50)
            )
            rows = result.scalars().all()

        return [
            AgentThoughtRow(
                seq=row.sequence_index,
                role=row.role,
                content=row.content or "",
                tool_name=row.tool_name or "",
                recorded_at=row.recorded_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_thoughts_tail DB query failed (non-fatal): %s", exc)
        return []

