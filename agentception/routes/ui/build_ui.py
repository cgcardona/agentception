from __future__ import annotations

"""UI routes: Ship / Mission Control page.

Endpoints
---------
GET  /ship                                  — redirect to /ship/{org}/{repo}/{first-initiative}
GET  /ship/{org}/{repo}/{initiative}        — full page (Mission Control)
GET  /ship/{org}/{repo}/{initiative}/board  — HTMX board partial (polled every 5 s)
GET  /ship/{org}/{repo}/{initiative}/tree   — JSON agent tree for latest active batch
GET  /ship/runs/{run_id}/tree              — JSON agent tree for a specific run's batch
GET  /ship/runs/{run_id}/stream            — SSE: structured events + thinking messages

The board shows all issues grouped by phase with live PR/agent-run status.
The inspector panel streams events from ``ac_agent_events`` and thinking
messages from ``ac_agent_messages`` for a selected agent run.  The hierarchy
panel renders the full agent tree (executive → coordinator → leaf) from the
most recently active batch for the current initiative.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from starlette.requests import Request

from typing import TypedDict

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.db.queries import (
    PhaseGroupRow,
    RunForIssueRow,
    RunTreeNodeRow,
    get_agent_events_tail,
    get_agent_thoughts_tail,
    get_initiatives,
    get_issues_grouped_by_phase,
    get_latest_active_batch_id,
    get_run_tree_by_batch_id,
    get_runs_for_issue_numbers,
    get_workflow_states_by_issue,
)
from agentception.readers.pipeline_config import read_pipeline_config
from ._shared import _TEMPLATES


class EnrichedIssueRow(TypedDict):
    """Issue row enriched with the most-recent agent run and deterministic swim lane.

    ``swim_lane`` is the canonical swim lane string computed by the workflow
    state machine.  It is derived from authoritative DB signals — not from
    Jinja2 logic — ensuring there is exactly one definition.

    Values: ``'todo'`` | ``'active'`` | ``'pr_open'`` | ``'reviewing'`` | ``'done'``
    """

    number: int
    title: str
    body_excerpt: str
    state: str
    url: str
    labels: list[str]
    depends_on: list[int]
    run: RunForIssueRow | None
    swim_lane: str
    pr_number: int | None


class EnrichedPhaseGroupRow(TypedDict):
    """PhaseGroupRow whose issues are EnrichedIssueRow (have a 'run' field)."""

    label: str
    issues: list[EnrichedIssueRow]
    locked: bool
    complete: bool
    depends_on: list[str]



logger = logging.getLogger(__name__)

router = APIRouter()


async def _initiative_patterns() -> list[str]:
    """Return the active project's initiative label patterns from pipeline-config.json."""
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
# Shared enrichment helper
# ---------------------------------------------------------------------------


async def _build_enriched_groups(
    repo: str,
    initiative: str,
) -> tuple[list[EnrichedPhaseGroupRow], int, int]:
    """Fetch and enrich all phase groups for *initiative*.

    Returns ``(enriched_groups, total_issue_count, open_issue_count)``.

    Swim lanes and PR numbers come exclusively from ``ac_issue_workflow_state``
    — the canonical persisted state written by the workflow state machine on
    every poller tick and immediately on ``build_report_done``.  There is no
    fallback; if a row is absent the issue is ``todo``.
    """
    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)
    all_issue_numbers = [i["number"] for g in groups for i in g["issues"]]
    open_issue_count = sum(
        1 for g in groups for i in g["issues"] if i["state"] != "closed"
    )
    runs, workflow_states = await asyncio.gather(
        get_runs_for_issue_numbers(all_issue_numbers),
        get_workflow_states_by_issue(all_issue_numbers, repo),
    )
    enriched: list[EnrichedPhaseGroupRow] = [
        EnrichedPhaseGroupRow(
            label=g["label"],
            issues=[
                EnrichedIssueRow(
                    number=i["number"],
                    title=i["title"],
                    body_excerpt=i["body_excerpt"],
                    state=i["state"],
                    url=i["url"],
                    labels=i["labels"],
                    depends_on=i["depends_on"],
                    run=runs.get(i["number"]),
                    swim_lane=(
                        workflow_states[i["number"]]["lane"]
                        if i["number"] in workflow_states
                        else "todo"
                    ),
                    pr_number=(
                        workflow_states[i["number"]].get("pr_number")
                        if i["number"] in workflow_states
                        else None
                    ),
                )
                for i in g["issues"]
            ],
            locked=g["locked"],
            complete=g["complete"],
            depends_on=g["depends_on"],
        )
        for g in groups
    ]
    return enriched, len(all_issue_numbers), open_issue_count


# ---------------------------------------------------------------------------
# GET /ship — redirect to first available initiative
# ---------------------------------------------------------------------------


@router.get("/ship", response_class=HTMLResponse, response_model=None)
async def ship_redirect() -> Response:
    """Redirect ``/ship`` to ``/ship/{repo}/{first-initiative}`` when initiatives exist.

    Falls through to /plan if none are found.
    """
    gh_repo = settings.gh_repo
    repo_name = gh_repo.split("/")[-1]
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(gh_repo, initiative_patterns=patterns)
    if initiatives:
        return RedirectResponse(url=f"/ship/{repo_name}/{initiatives[0]}", status_code=302)
    return RedirectResponse(url="/plan", status_code=302)


# ---------------------------------------------------------------------------
# GET /ship/{repo}/{initiative} — full Mission Control page
# ---------------------------------------------------------------------------


@router.get("/ship/{repo}/{initiative}", response_class=HTMLResponse, response_model=None)
async def build_page(
    request: Request,
    repo: str,
    initiative: str,
) -> Response:
    """Render the Mission Control Ship page scoped to *repo/initiative*."""
    gh_repo = settings.gh_repo
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(gh_repo, initiative_patterns=patterns)
    enriched_groups, total_issues, open_issues = await _build_enriched_groups(gh_repo, initiative)
    return _TEMPLATES.TemplateResponse(
        request,
        "build.html",
        {
            "repo": gh_repo,
            "initiative": initiative,
            "initiatives": initiatives,
            "groups": enriched_groups,
            "total_issues": total_issues,
            "open_issues": open_issues,
        },
    )


# ---------------------------------------------------------------------------
# GET /ship/{repo}/{initiative}/board — HTMX board partial (polled every 5 s)
# ---------------------------------------------------------------------------


@router.get("/ship/{repo}/{initiative}/board", response_class=HTMLResponse)
async def build_board_partial(
    request: Request,
    repo: str,
    initiative: str,
) -> HTMLResponse:
    """Return the phase-grouped board as an HTML partial for HTMX polling."""
    gh_repo = settings.gh_repo
    enriched_groups, _, _ = await _build_enriched_groups(gh_repo, initiative)
    return _TEMPLATES.TemplateResponse(
        request,
        "_build_board.html",
        {
            "groups": enriched_groups,
            "repo": gh_repo,
            "initiative": initiative,
        },
    )


# ---------------------------------------------------------------------------
# GET /ship/runs/{run_id}/stream — SSE inspector stream
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

        ping_counter += 1
        if ping_counter % 10 == 0:
            yield 'data: {"t":"ping"}\n\n'

        await asyncio.sleep(2)


@router.get("/ship/runs/{run_id}/tree", response_class=Response, response_model=None)
async def agent_run_tree(run_id: str) -> Response:
    """Return the full agent tree for the batch containing *run_id*.

    Looks up the run's ``batch_id``, then fetches all sibling runs in that
    batch and returns them as a flat list ordered by spawn time.  The client
    assembles the tree via ``parent_run_id`` references.

    Returns ``{"nodes": [], "batch_id": null}`` when the run is not found or
    has no batch.
    """
    batch_id: str | None = None
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.batch_id).where(ACAgentRun.id == run_id)
            )
            raw = result.scalar_one_or_none()
            batch_id = str(raw) if raw is not None else None
    except Exception:
        batch_id = None

    if not batch_id:
        return JSONResponse({"nodes": [], "batch_id": None})

    nodes = await get_run_tree_by_batch_id(batch_id)
    return JSONResponse({"nodes": nodes, "batch_id": batch_id})


@router.get("/ship/{repo}/{initiative}/tree", response_class=Response, response_model=None)
async def initiative_active_tree(repo: str, initiative: str) -> Response:
    """Return the agent tree for the most recently active batch under *repo/initiative*.

    Used by the build board to populate the hierarchy panel when no specific
    issue is selected.  Falls back to an empty tree when there are no runs.
    """
    gh_repo = settings.gh_repo
    groups = await get_issues_grouped_by_phase(gh_repo, initiative=initiative)
    # Only open issues drive the hierarchy — closed issues' stale runs must
    # never surface a ghost agent in the panel.
    open_issue_numbers = [
        i["number"] for g in groups for i in g["issues"] if i["state"] != "closed"
    ]
    batch_id = await get_latest_active_batch_id(issue_numbers=open_issue_numbers)
    if not batch_id:
        return JSONResponse({"nodes": [], "batch_id": None})
    nodes = await get_run_tree_by_batch_id(batch_id)
    return JSONResponse({"nodes": nodes, "batch_id": batch_id})


@router.get("/ship/runs/{run_id}/stream")
async def agent_stream(run_id: str) -> StreamingResponse:
    """SSE stream of structured events + thinking for the inspector panel.

    Clients open this once when the user clicks an issue card.  The stream
    runs until the client closes it.

    Args:
        run_id: The agent run id (e.g. ``issue-938``).

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
