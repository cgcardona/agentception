from __future__ import annotations

"""MCP Log tools — append-only telemetry events.

Every function in this module appends an event to ``ac_agent_events``.
None of these tools change run state.  They are idempotent in the sense
that duplicate calls produce duplicate events (safe to retry).

Event type catalogue
--------------------
``step_start``  — agent is entering a named execution step
``blocker``     — agent is stalled on an external dependency
``decision``    — agent made a significant architectural choice
``message``     — free-form informational note
``error``       — unrecoverable failure or crash (semantic; use before cancelling)

Rules
-----
- These tools NEVER change run state.  State transitions live in build_commands.py.
- All calls are best-effort — a DB failure returns ``ok: False`` but never
  raises an exception that would abort the agent.
"""

import logging

from agentception.db.persist import persist_agent_event

logger = logging.getLogger(__name__)


async def log_run_step(
    issue_number: int,
    step_name: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
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


async def log_run_blocker(
    issue_number: int,
    description: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record that the agent encountered a blocker.

    This tool appends a blocker event only.  To also transition the run to
    ``blocked`` state (preventing other Dispatchers from re-claiming it), call
    ``build_block_run`` separately.

    Was: ``build_report_blocker``.

    Args:
        issue_number: GitHub issue number the agent is working on.
        description: What is blocking the agent.
        agent_run_id: Optional run id.

    Returns:
        ``{"ok": True, "event": "blocker"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="blocker",
        payload={"description": description},
        agent_run_id=agent_run_id,
    )
    logger.warning("⚠️ log_run_blocker: issue=%d — %s", issue_number, description)
    return {"ok": True, "event": "blocker"}


async def log_run_decision(
    issue_number: int,
    decision: str,
    rationale: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record a significant architectural or implementation decision.

    Was: ``build_report_decision``.

    Args:
        issue_number: GitHub issue number the agent is working on.
        decision: One-sentence description of the decision made.
        rationale: Why this decision was made.
        agent_run_id: Optional run id.

    Returns:
        ``{"ok": True, "event": "decision"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="decision",
        payload={"decision": decision, "rationale": rationale},
        agent_run_id=agent_run_id,
    )
    logger.info(
        "✅ log_run_decision: issue=%d decision=%r", issue_number, decision
    )
    return {"ok": True, "event": "decision"}


async def log_run_message(
    issue_number: int,
    message: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Append a free-form message to the agent's event log.

    Use this for any noteworthy information that doesn't fit a structured
    event type (step, blocker, decision).  Never use this as a substitute
    for a specific event type.

    Args:
        issue_number: GitHub issue number the agent is working on.
        message: The message text to log.
        agent_run_id: Optional run id.

    Returns:
        ``{"ok": True, "event": "message"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="message",
        payload={"message": message},
        agent_run_id=agent_run_id,
    )
    logger.info("✅ log_run_message: issue=%d message=%r", issue_number, message[:80])
    return {"ok": True, "event": "message"}


async def log_run_error(
    issue_number: int,
    error: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record an unrecoverable error or crash with semantic distinction.

    Use this instead of :func:`log_run_message` when the agent is aborting
    due to an exception, API failure, or any condition it cannot recover from.
    The dashboard surfaces ``error`` events differently from free-form messages
    so operators can triage failures at a glance.

    After calling this tool, transition the run to ``cancelled`` or ``stopped``
    by calling ``build_cancel_run`` or ``build_stop_run`` as appropriate.

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
