from __future__ import annotations

"""UI routes: Ship / Mission Control page.

Endpoints
---------
GET  /ship                          — redirect to /ship/{first-initiative}
GET  /ship/{initiative}             — full page (Mission Control)
GET  /ship/{initiative}/board       — HTMX board partial (polled every 5 s)
GET  /ship/{initiative}/tree        — JSON agent tree for latest active batch
GET  /ship/runs/{run_id}/tree       — JSON agent tree for a specific run's batch
GET  /ship/runs/{run_id}/stream     — SSE: structured events + thinking messages

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
    OpenPRForIssueRow,
    PhaseGroupRow,
    RunForIssueRow,
    RunTreeNodeRow,
    WorkflowStateRow,
    get_agent_events_tail,
    get_agent_thoughts_tail,
    get_initiatives,
    get_issues_grouped_by_phase,
    get_latest_active_batch_id,
    get_open_prs_by_issue,
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


def _compute_swim_lane(
    issue_state: str,
    run: RunForIssueRow | None,
    open_pr: OpenPRForIssueRow | None,
) -> str:
    """Compute the deterministic swim lane for a board issue card.

    This is the single canonical definition of swim lane assignment.
    There must be no other place in the codebase that decides which lane
    a card belongs in — Jinja2 templates, JS, and API responses all read
    the ``swim_lane`` field produced here.

    Priority (highest wins):
    1. DONE      — ``ac_issues.state == 'closed'``  (GitHub is the source of truth)
    2. REVIEWING — open PR in ``ac_pull_requests`` AND active reviewer agent run
    3. PR_OPEN   — open PR in ``ac_pull_requests``  (authoritative; not ac_agent_runs.pr_number)
    4. ACTIVE    — active agent run with no open PR yet
    5. TODO      — none of the above
    """
    if issue_state == "closed":
        return "done"
    if open_pr is not None:
        if run is not None and run["agent_status"] == "reviewing":
            return "reviewing"
        return "pr_open"
    if run is not None and run["agent_status"] in (
        "implementing",
        "pending_launch",
        "stale",
        "reviewing",
    ):
        return "active"
    return "todo"

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
    Centralises the DB fan-out so both the full page and the HTMX partial
    share a single implementation.

    Reads swim lanes from the canonical ``ac_issue_workflow_state`` table.
    Falls back to ad-hoc ``_compute_swim_lane()`` for issues that haven't
    been computed yet (graceful dual-run during migration).
    """
    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)
    all_issue_numbers = [i["number"] for g in groups for i in g["issues"]]
    open_issue_count = sum(
        1 for g in groups for i in g["issues"] if i["state"] != "closed"
    )
    runs, open_prs, workflow_states = await asyncio.gather(
        get_runs_for_issue_numbers(all_issue_numbers),
        get_open_prs_by_issue(all_issue_numbers, repo),
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
                    swim_lane=_resolve_swim_lane(
                        i["number"],
                        i["state"],
                        runs.get(i["number"]),
                        open_prs.get(i["number"]),
                        workflow_states.get(i["number"]),
                    ),
                    pr_number=_resolve_pr_number(
                        runs.get(i["number"]),
                        workflow_states.get(i["number"]),
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


def _resolve_swim_lane(
    issue_number: int,
    issue_state: str,
    run: RunForIssueRow | None,
    open_pr: OpenPRForIssueRow | None,
    workflow_state: WorkflowStateRow | None,
) -> str:
    """Return the canonical swim lane, preferring the persisted workflow state.

    During dual-run / migration, falls back to ``_compute_swim_lane()``
    for issues without a persisted state row yet.
    """
    if workflow_state is not None:
        return workflow_state["lane"]
    return _compute_swim_lane(issue_state, run, open_pr)


def _resolve_pr_number(
    run: RunForIssueRow | None,
    workflow_state: WorkflowStateRow | None,
) -> int | None:
    """Return the best PR number for an issue.

    Prefers the canonical workflow state (linker-derived), falls back to
    the agent run's ``pr_number`` for issues without a persisted state.
    """
    if workflow_state is not None and workflow_state.get("pr_number"):
        return workflow_state["pr_number"]
    if run is not None and run.get("pr_number"):
        return run["pr_number"]
    return None


# ---------------------------------------------------------------------------
# GET /ship — redirect to first available initiative
# ---------------------------------------------------------------------------


@router.get("/ship", response_class=HTMLResponse, response_model=None)
async def ship_redirect() -> Response:
    """Redirect ``/ship`` to ``/ship/{first-initiative}`` when initiatives exist.

    Falls through to /plan if none are found.
    """
    repo = settings.gh_repo
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(repo, initiative_patterns=patterns)
    if initiatives:
        return RedirectResponse(url=f"/ship/{initiatives[0]}", status_code=302)
    return RedirectResponse(url="/plan", status_code=302)


# ---------------------------------------------------------------------------
# GET /ship/{initiative} — full Mission Control page
# ---------------------------------------------------------------------------


@router.get("/ship/{initiative}", response_class=HTMLResponse, response_model=None)
async def build_page(
    request: Request,
    initiative: str,
) -> Response:
    """Render the Mission Control Ship page scoped to *initiative*."""
    repo = settings.gh_repo
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(repo, initiative_patterns=patterns)
    enriched_groups, total_issues, open_issues = await _build_enriched_groups(repo, initiative)
    return _TEMPLATES.TemplateResponse(
        request,
        "build.html",
        {
            "repo": repo,
            "initiative": initiative,
            "initiatives": initiatives,
            "groups": enriched_groups,
            "total_issues": total_issues,
            "open_issues": open_issues,
        },
    )


# ---------------------------------------------------------------------------
# GET /ship/{initiative}/board — HTMX board partial (polled every 5 s)
# ---------------------------------------------------------------------------


@router.get("/ship/{initiative}/board", response_class=HTMLResponse)
async def build_board_partial(
    request: Request,
    initiative: str,
) -> HTMLResponse:
    """Return the phase-grouped board as an HTML partial for HTMX polling."""
    repo = settings.gh_repo
    enriched_groups, _, _ = await _build_enriched_groups(repo, initiative)
    return _TEMPLATES.TemplateResponse(
        request,
        "_build_board.html",
        {
            "groups": enriched_groups,
            "repo": repo,
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


@router.get("/ship/{initiative}/tree", response_class=Response, response_model=None)
async def initiative_active_tree(initiative: str) -> Response:
    """Return the agent tree for the most recently active batch under *initiative*.

    Used by the build board to populate the hierarchy panel when no specific
    issue is selected.  Falls back to an empty tree when there are no runs.
    """
    repo = settings.gh_repo
    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)
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
