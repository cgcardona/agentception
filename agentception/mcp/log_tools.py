from __future__ import annotations

"""MCP Log tools — append-only telemetry events.

Every function in this module appends an event to ``ac_agent_events``.
None of these tools change run state.  They are idempotent in the sense
that duplicate calls produce duplicate events (safe to retry).

Event type catalogue
--------------------
``step_start``  — agent is entering a named execution step
``error``       — unrecoverable failure or crash (semantic; use before cancelling)
``file_edit``   — a file-mutating tool call completed successfully

Rules
-----
- These tools NEVER change run state.  State transitions live in build_commands.py.
- All calls are best-effort — a DB failure returns ``ok: False`` but never
  raises an exception that would abort the agent.
"""

import logging

from agentception.types import JsonValue

from agentception.db.persist import persist_agent_event
from agentception.models import FileEditEvent

logger = logging.getLogger(__name__)


async def log_run_step(
    issue_number: int,
    step_name: str,
    agent_run_id: str | None = None,
) -> dict[str, JsonValue]:
    """Record that the agent is starting a named execution step.

    Was: ``build_report_step``.

    Args:
        issue_number: GitHub issue number the agent is working on.
        step_name: Human-readable step label (e.g. "Reading codebase").
        agent_run_id: Optional run id (e.g. "issue-938").

    Returns:
        ``{"ok": True, "event": "step_start"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="step_start",
        payload={"step": step_name},
        agent_run_id=agent_run_id,
    )
    logger.info("✅ log_run_step: issue=%d step=%r", issue_number, step_name)
    return {"ok": True, "event": "step_start"}


async def log_file_edit_event(
    issue_number: int,
    event: FileEditEvent,
    agent_run_id: str | None = None,
) -> None:
    """Persist a file_edit agent event so the inspector SSE stream picks it up.

    The payload is the FileEditEvent serialised to a dict.  The SSE generator
    in build_ui.py forwards all event_types generically, so no change there
    is required.
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="file_edit",
        payload=event.model_dump(mode="json"),
        agent_run_id=agent_run_id,
    )
    logger.info(
        "✅ log_file_edit_event: issue=%d path=%r lines_omitted=%d",
        issue_number,
        event.path,
        event.lines_omitted,
    )


async def log_run_error(
    issue_number: int,
    error: str,
    agent_run_id: str | None = None,
) -> dict[str, JsonValue]:
    """Record an unrecoverable error or crash with semantic distinction.

    Use this when the agent is aborting due to an exception, API failure,
    or any condition it cannot recover from.  The dashboard surfaces
    ``error`` events differently from other event types so operators can
    triage failures at a glance.

    After calling this tool, transition the run to ``cancelled``
    by calling ``build_cancel_run``.

    Args:
        issue_number: GitHub issue number the agent is working on.
        error: Human-readable description of the failure (include exception
               type and message where available).
        agent_run_id: Optional run id (e.g. ``"issue-938"``).

    Returns:
        ``{"ok": True, "event": "error"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="error",
        payload={"error": error},
        agent_run_id=agent_run_id,
    )
    logger.error("❌ log_run_error: issue=%d — %s", issue_number, error)
    return {"ok": True, "event": "error"}
