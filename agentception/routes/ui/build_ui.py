from __future__ import annotations

"""UI routes: Build / Mission Control page.

Endpoints
---------
GET  /build                      — full page (Mission Control)
GET  /build/board                — HTMX board partial (polled every 10 s)
GET  /build/agent/{run_id}/stream — SSE: structured events + thinking messages

The board shows all issues grouped by phase with live PR/agent-run status.
The inspector panel streams events from ``ac_agent_events`` and thinking
messages from ``ac_agent_messages`` for a selected agent run.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import Request

from typing import TypedDict

from agentception.config import settings
from agentception.db.queries import (
    PhaseGroupRow,
    RunForIssueRow,
    get_agent_events_tail,
    get_agent_thoughts_tail,
    get_initiatives,
    get_issues_grouped_by_phase,
    get_runs_for_issue_numbers,
)
from agentception.readers.pipeline_config import read_pipeline_config
from ._shared import _TEMPLATES


class EnrichedIssueRow(TypedDict):
    """Issue row enriched with the most-recent agent run attached."""

    number: int
    title: str
    state: str
    url: str
    labels: list[str]
    depends_on: list[int]
    run: RunForIssueRow | None


class EnrichedPhaseGroupRow(TypedDict):
    """PhaseGroupRow whose issues are EnrichedIssueRow (have a 'run' field)."""

    label: str
    issues: list[EnrichedIssueRow]
    locked: bool
    complete: bool
    depends_on: list[str]

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Role catalogue (derived from .cursor/roles/ on disk)
# ---------------------------------------------------------------------------

_ROLES_DIR = Path(__file__).parent.parent.parent.parent / ".cursor" / "roles"

_ROLE_GROUPS: dict[str, list[str]] = {
    "C-Suite": ["ceo", "cto", "cpo", "coo", "cfo", "cmo", "cdo", "ciso"],
    "Coordinators": [
        "product-coordinator", "infrastructure-coordinator", "platform-coordinator", "ml-coordinator",
        "mobile-coordinator", "data-coordinator", "design-coordinator", "security-coordinator",
    ],
    "Engineering": [
        "python-developer", "typescript-developer", "frontend-developer",
        "full-stack-developer", "api-developer", "go-developer", "rust-developer",
        "android-developer", "ios-developer", "mobile-developer",
        "react-developer", "rails-developer", "systems-programmer",
        "database-architect", "devops-engineer", "site-reliability-engineer",
        "security-engineer",
    ],
    "Specialists": [
        "architect", "ml-engineer", "ml-researcher", "data-engineer",
        "data-scientist", "engineering-coordinator", "qa-coordinator", "test-engineer",
        "technical-writer", "muse-specialist", "pr-reviewer", "coordinator",
    ],
}


def _available_roles() -> dict[str, list[str]]:
    """Return role groups filtered to roles that actually exist on disk."""
    out: dict[str, list[str]] = {}
    for group, roles in _ROLE_GROUPS.items():
        present = [r for r in roles if (_ROLES_DIR / f"{r}.md").exists()]
        if present:
            out[group] = present
    return out


async def _initiative_patterns() -> list[str]:
    """Return the active project's initiative label patterns from pipeline-config.json.

    Looks up the project whose ``gh_repo`` matches ``settings.gh_repo`` and
    returns its ``initiative_labels`` list.  Falls back to ``[]`` (which
    triggers the legacy ``phase-N`` heuristic in ``get_initiatives``) when the
    config is absent, unreadable, or has no matching project entry.
    """
    try:
        cfg = await read_pipeline_config()
        for project in cfg.projects:
            if project.gh_repo == settings.gh_repo:
                return project.initiative_labels
        return []
    except Exception as exc:
        logger.warning("⚠️ Could not read initiative patterns from pipeline config: %s", exc)
        return []


# ---------------------------------------------------------------------------
# /build — full Mission Control page
# ---------------------------------------------------------------------------


@router.get("/build", response_class=HTMLResponse, response_model=None)
async def build_page(
    request: Request,
    initiative: str | None = Query(default=None),
) -> Response:
    """Render the Mission Control build page.

    When no *initiative* query param is provided, redirects to the first
    available initiative so the board is always scoped.  Falls through to
    the unscoped view only when the DB has no initiative-labelled issues at all.
    """
    repo = settings.gh_repo
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(repo, initiative_patterns=patterns)

    # Auto-select the first initiative when none is specified.
    if not initiative and initiatives:
        return RedirectResponse(
            url=f"/build?initiative={initiatives[0]}", status_code=302
        )

    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)

    all_issue_numbers = [i["number"] for g in groups for i in g["issues"]]
    runs = await get_runs_for_issue_numbers(all_issue_numbers)

    enriched_groups: list[EnrichedPhaseGroupRow] = [
        EnrichedPhaseGroupRow(
            label=g["label"],
            issues=[
                EnrichedIssueRow(
                    number=i["number"],
                    title=i["title"],
                    state=i["state"],
                    url=i["url"],
                    labels=i["labels"],
                    depends_on=i["depends_on"],
                    run=runs.get(i["number"]),
                )
                for i in g["issues"]
            ],
            locked=g["locked"],
            complete=g["complete"],
            depends_on=g["depends_on"],
        )
        for g in groups
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "build.html",
        {
            "repo": repo,
            "initiative": initiative or "",
            "initiatives": initiatives,
            "groups": enriched_groups,
            "role_groups": _available_roles(),
            "total_issues": len(all_issue_numbers),
        },
    )


# ---------------------------------------------------------------------------
# /build/board — HTMX board partial (polled every 10 s)
# ---------------------------------------------------------------------------


@router.get("/build/board", response_class=HTMLResponse)
async def build_board_partial(
    request: Request,
    initiative: str | None = Query(default=None),
) -> HTMLResponse:
    """Return the phase-grouped board as an HTML partial for HTMX polling."""
    repo = settings.gh_repo
    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)

    all_issue_numbers = [i["number"] for g in groups for i in g["issues"]]
    runs = await get_runs_for_issue_numbers(all_issue_numbers)

    enriched_groups: list[EnrichedPhaseGroupRow] = [
        EnrichedPhaseGroupRow(
            label=g["label"],
            issues=[
                EnrichedIssueRow(
                    number=i["number"],
                    title=i["title"],
                    state=i["state"],
                    url=i["url"],
                    labels=i["labels"],
                    depends_on=i["depends_on"],
                    run=runs.get(i["number"]),
                )
                for i in g["issues"]
            ],
            locked=g["locked"],
            complete=g["complete"],
            depends_on=g["depends_on"],
        )
        for g in groups
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "_build_board.html",
        {
            "groups": enriched_groups,
            "repo": repo,
            "initiative": initiative or "",
        },
    )


# ---------------------------------------------------------------------------
# /build/agent/{run_id}/stream — SSE inspector stream
# ---------------------------------------------------------------------------


async def _inspector_sse(run_id: str) -> AsyncGenerator[str, None]:
    """Yield SSE events for the inspector panel.

    Interleaves structured MCP events (``ac_agent_events``) and raw thinking
    messages (``ac_agent_messages``) in near-real-time.  Polls DB every 2 s.

    Event shapes::

        data: {"t": "event", "event_type": "step_start", "payload": {...}, "recorded_at": "..."}
        data: {"t": "thought", "role": "thinking", "content": "...", "recorded_at": "..."}
        data: {"t": "ping"}   -- keepalive every ~20 s
    """
    last_event_id = 0
    last_thought_seq = -1
    ping_counter = 0

    while True:
        # Structured events
        events = await get_agent_events_tail(run_id, after_id=last_event_id)
        for ev in events:
            last_event_id = max(last_event_id, int(ev["id"]))
            payload = json.dumps(
                {
                    "t": "event",
                    "event_type": ev["event_type"],
                    "payload": json.loads(ev["payload"]),
                    "recorded_at": ev["recorded_at"],
                }
            )
            yield f"data: {payload}\n\n"

        # Raw thinking messages
        thoughts = await get_agent_thoughts_tail(
            run_id, after_seq=last_thought_seq
        )
        for thought in thoughts:
            last_thought_seq = max(last_thought_seq, int(thought["seq"]))
            payload = json.dumps(
                {
                    "t": "thought",
                    "role": thought["role"],
                    "content": thought["content"],
                    "recorded_at": thought["recorded_at"],
                }
            )
            yield f"data: {payload}\n\n"

        # Keepalive ping every ~20 s (10 × 2 s sleep)
        ping_counter += 1
        if ping_counter % 10 == 0:
            yield 'data: {"t":"ping"}\n\n'

        await asyncio.sleep(2)


@router.get("/build/agent/{run_id}/stream")
async def agent_stream(run_id: str) -> StreamingResponse:
    """SSE stream of structured events + thinking for the inspector panel.

    Clients open this once when the user clicks an issue card.  The stream
    runs until the client closes it.

    Args:
        run_id: The agent run id (worktree basename, e.g. ``issue-938``).

    Returns:
        ``text/event-stream`` SSE response.
    """
    return StreamingResponse(
        _inspector_sse(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/build/agent/{run_id}/message — send a user message to an agent
# ---------------------------------------------------------------------------


class _AgentMessageBody(BaseModel):
    content: str


@router.post("/api/build/agent/{run_id}/message", response_model=None)
async def post_agent_message(run_id: str, body: _AgentMessageBody) -> Response:
    """Append a user message to an agent run's transcript.

    The message is stored in ``agent_messages`` with ``role='user'`` and
    immediately picked up by the inspector's SSE stream on the next 2-second
    poll.  This lets operators leave context for a running agent and see it
    reflected in the inspector's chain-of-thought area.

    Returns 404 if the run_id is not found.
    """
    from agentception.db.engine import get_session
    from agentception.db.models import ACAgentMessage, ACAgentRun
    from sqlalchemy import select, func

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must be non-empty")

    try:
        async with get_session() as session:
            run_exists = await session.scalar(
                select(func.count()).select_from(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            if not run_exists:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            seq_result = await session.scalar(
                select(func.coalesce(func.max(ACAgentMessage.sequence_index), -1)).where(
                    ACAgentMessage.agent_run_id == run_id
                )
            )
            next_seq = int(seq_result) + 1 if seq_result is not None else 0

            import datetime

            session.add(
                ACAgentMessage(
                    agent_run_id=run_id,
                    role="user",
                    content=content,
                    sequence_index=next_seq,
                    recorded_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ post_agent_message failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save message") from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/build/agent/{run_id}/stop — mark a run as done
# ---------------------------------------------------------------------------


@router.post("/api/build/agent/{run_id}/stop", response_model=None)
async def stop_agent(run_id: str) -> Response:
    """Mark an agent run as DONE so the board no longer shows it as active.

    Does not remove the worktree (use the control panel kill action for that).
    This is a lightweight "I'm done watching this" action that removes the
    run from the active-agent display so a fresh agent can be dispatched.

    Returns 404 if the run_id is not found.
    """
    from agentception.db.engine import get_session
    from agentception.db.models import ACAgentRun
    from sqlalchemy import select

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            run.status = "DONE"
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ stop_agent failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to stop agent") from exc

    return Response(status_code=204)
