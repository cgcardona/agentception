"""UI routes: telemetry D3 dashboard and trend partial."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.telemetry import aggregate_waves
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/telemetry", response_class=HTMLResponse)
async def telemetry_page(request: Request) -> HTMLResponse:
    """Telemetry D3 dashboard — wave history + pipeline trend.

    Data sources:
    - ``aggregate_waves()`` — reads worktrees grouped by BATCH_ID
      into WaveSummary objects for D3 charts.
    - ``get_pipeline_trend()`` — reads ``ac_pipeline_snapshots`` (Postgres)
      for the pipeline trend multi-line chart.

    All wave/trend data is serialised to JSON and embedded in the page via
    ``<script type="application/json">`` tags so D3 can read them without
    HTML attribute quoting issues.  Both sources degrade to empty on failure.
    """
    import json as _json
    from agentception.db.queries import get_pipeline_trend

    waves, trend = await asyncio.gather(
        aggregate_waves(),
        get_pipeline_trend(hours=24, limit=500),
    )

    all_issues: set[int] = set()
    for wave in waves:
        all_issues.update(wave.issues_worked)
    total_issues = len(all_issues)
    total_cost_usd = round(sum(w.estimated_cost_usd for w in waves), 4)
    total_agents = sum(len(w.agents) for w in waves)
    total_waves = len(waves)

    # Role counts across all agents in all waves (for KPI + D3 donut seed).
    role_counts: dict[str, int] = {}
    for wave in waves:
        for agent in wave.agents:
            role_counts[agent.role] = role_counts.get(agent.role, 0) + 1

    # Serialise for D3 — embed as application/json script tags (never in x-data).
    waves_json: str = _json.dumps([w.model_dump() for w in waves])
    trend_json: str = _json.dumps(trend)

    return _TEMPLATES.TemplateResponse(
        request,
        "telemetry.html",
        {
            # KPI tiles (server-rendered).
            "total_waves": total_waves,
            "total_issues": total_issues,
            "total_cost_usd": total_cost_usd,
            "total_agents": total_agents,
            "role_counts": role_counts,
            # Wave table (Jinja loop — same data as D3, just typed objects).
            "waves": waves,
            # JSON blobs for D3 (injected as application/json script tags).
            "waves_json": waves_json,
            "trend_json": trend_json,
            "trend_count": len(trend),
        },
    )


@router.get("/htmx/telemetry/trend", response_class=HTMLResponse)
async def telemetry_trend_partial(request: Request) -> HTMLResponse:
    """HTMX partial — refreshes the pipeline trend JSON blob on the telemetry page.

    Returns a ``<script type="application/json">`` replacement tag so the
    browser can swap it in and the Alpine component can trigger a D3 re-render.
    """
    import json as _json
    from agentception.db.queries import get_pipeline_trend

    from agentception.db.queries import PipelineTrendRow
    trend: list[PipelineTrendRow] = []
    try:
        trend = await get_pipeline_trend(hours=24, limit=500)
    except Exception as exc:  # pragma: no cover
        logger.warning("⚠️ telemetry_trend_partial: DB failure: %s", exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "_telemetry_trend.html",
        {"trend_json": _json.dumps(trend)},
    )
