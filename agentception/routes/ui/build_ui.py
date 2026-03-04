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
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import Request

from agentception.config import settings
from agentception.db.queries import (
    get_agent_events_tail,
    get_agent_thoughts_tail,
    get_initiatives,
    get_issues_grouped_by_phase,
    get_runs_for_issue_numbers,
)
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Role catalogue (derived from .cursor/roles/ on disk)
# ---------------------------------------------------------------------------

_ROLES_DIR = Path(__file__).parent.parent.parent.parent / ".cursor" / "roles"

_ROLE_GROUPS: dict[str, list[str]] = {
    "C-Suite": ["ceo", "cto", "cpo", "coo", "cfo", "cmo", "cdo", "ciso"],
    "VPs": [
        "vp-product", "vp-infrastructure", "vp-platform", "vp-ml",
        "vp-mobile", "vp-data", "vp-design", "vp-security",
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
    the legacy unscoped view only when the DB has no initiative-labelled
    issues at all.
    """
    repo = settings.gh_repo
    initiatives = await get_initiatives(repo)

    # Auto-select the first initiative when none is specified.
    if not initiative and initiatives:
        return RedirectResponse(
            url=f"/build?initiative={initiatives[0]}", status_code=302
        )

    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)

    all_issue_numbers = [i["number"] for g in groups for i in g["issues"]]
    runs = await get_runs_for_issue_numbers(all_issue_numbers)

    for group in groups:
        for issue in group["issues"]:
            issue["run"] = runs.get(issue["number"])

    return _TEMPLATES.TemplateResponse(
        "build.html",
        {
            "request": request,
            "repo": repo,
            "initiative": initiative or "",
            "initiatives": initiatives,
            "groups": groups,
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

    for group in groups:
        for issue in group["issues"]:
            issue["run"] = runs.get(issue["number"])

    return _TEMPLATES.TemplateResponse(
        "_build_board.html",
        {
            "request": request,
            "groups": groups,
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
                    "payload": ev["payload"],
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
