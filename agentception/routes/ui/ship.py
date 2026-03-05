"""UI routes: Ship page — PR-centric deployment view.

Endpoints
---------
GET  /ship                        — full page (PR board, scoped by batch/initiative)
GET  /ship/board                  — HTMX board partial (polled every 10 s)
GET  /ship/agent/{run_id}/stream  — SSE inspector alias (delegates to build stream)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request

from agentception.config import settings
from agentception.db.queries import (
    ShipPhaseGroupRow,
    get_initiatives,
    get_prs_grouped_by_phase,
)
from agentception.routes.ui.build_ui import (
    _initiative_patterns,
    agent_stream as _build_agent_stream,
)
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# /ship — full PR board page
# ---------------------------------------------------------------------------


@router.get("/ship", response_class=HTMLResponse, response_model=None)
async def ship_page(
    request: Request,
    initiative: str | None = Query(default=None),
    batch: str | None = Query(default=None),
) -> Response:
    """Render the Ship page — a PR-centric view of the deployment pipeline.

    Scoped by ``?initiative=`` and/or ``?batch=`` query params.  When no
    initiative is provided and at least one exists, redirects to the first
    so the board is always scoped.
    """
    repo = settings.gh_repo
    patterns = await _initiative_patterns()
    initiatives = await get_initiatives(repo, initiative_patterns=patterns)

    if not initiative and initiatives and not batch:
        return RedirectResponse(
            url=f"/ship?initiative={initiatives[0]}", status_code=302
        )

    groups: list[ShipPhaseGroupRow] = await get_prs_grouped_by_phase(
        repo, initiative=initiative, batch_id=batch
    )

    total_prs = sum(len(g["prs"]) for g in groups)

    return _TEMPLATES.TemplateResponse(
        request,
        "ship.html",
        {
            "repo": repo,
            "initiative": initiative or "",
            "initiatives": initiatives,
            "batch": batch or "",
            "groups": groups,
            "total_prs": total_prs,
        },
    )


# ---------------------------------------------------------------------------
# /ship/board — HTMX board partial (polled every 10 s)
# ---------------------------------------------------------------------------


@router.get("/ship/board", response_class=HTMLResponse)
async def ship_board_partial(
    request: Request,
    initiative: str | None = Query(default=None),
    batch: str | None = Query(default=None),
) -> HTMLResponse:
    """Return the PR board grouped by phase as an HTML partial for HTMX polling."""
    repo = settings.gh_repo
    groups: list[ShipPhaseGroupRow] = await get_prs_grouped_by_phase(
        repo, initiative=initiative, batch_id=batch
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/ship_board.html",
        {
            "groups": groups,
            "repo": repo,
            "initiative": initiative or "",
            "batch": batch or "",
        },
    )


# ---------------------------------------------------------------------------
# /ship/agent/{run_id}/stream — SSE inspector alias
# ---------------------------------------------------------------------------


@router.get("/ship/agent/{run_id}/stream")
async def ship_agent_stream(run_id: str) -> Response:
    """SSE stream alias — delegates to the build board inspector stream.

    Allows the Ship page inspector panel to reuse the same event stream as
    the Build board without duplicating the SSE generator logic.
    """
    return await _build_agent_stream(run_id)
