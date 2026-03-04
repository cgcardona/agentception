"""UI routes: dashboard overview and issue analysis partial."""
from __future__ import annotations

import asyncio
import json as _json
import logging
from itertools import groupby as _groupby

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.config import settings as _settings
from agentception.intelligence.analyzer import IssueAnalysis, analyze_issue
from agentception.intelligence.guards import PRViolation, detect_out_of_order_prs
from agentception.intelligence.pipeline_lanes import PhaseLane, compute_phase_lanes
from agentception.intelligence.scaling import ScalingRecommendation, compute_recommendation
from agentception.models import PipelineState
from agentception.poller import get_state, tick as _poller_tick
from agentception.readers.pipeline_config import read_pipeline_config
from agentception.telemetry import WaveSummary, aggregate_waves
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    """Dashboard overview — live agent hierarchy tree and GitHub board sidebar.

    Server-renders everything knowable at request time so the page is
    fully painted before any client-side JavaScript runs.  Only the SSE
    stream (live updates) and user interactions (button clicks) require
    client-side code after initial render.

    Data sources:
    - ``state`` — in-memory PipelineState from the background poller.
    - ``board_issues`` — from ``ac_issues`` (Postgres) via poller tick.
    - ``scaling_advice`` — computed synchronously from wave history + config.
    - ``pr_violations`` — detected from open PRs via gh CLI.
    - ``poller_paused`` — sentinel file presence check (no network call).
    - Phase labels and pin — from ``pipeline-config.json`` + memory store.
    """
    # Fire an immediate tick in the background so the SSE stream delivers
    # fresh data within seconds of the page loading — eliminates up-to-5s
    # staleness on hard refresh without adding latency to the initial render.
    asyncio.get_event_loop().create_task(_poller_tick())

    state = get_state() or PipelineState.empty()
    all_phase_labels: list[str] = []
    label_is_pinned: bool = False
    active_org: str | None = None

    try:
        pipeline_cfg = await read_pipeline_config()
        all_phase_labels = pipeline_cfg.active_labels_order
    except Exception as exc:
        logger.warning("⚠️ Could not read pipeline config: %s", exc)

    try:
        _cfg_path = _settings.ac_dir / "pipeline-config.json"
        if _cfg_path.exists():
            _raw_cfg: object = _json.loads(_cfg_path.read_text(encoding="utf-8"))
            if isinstance(_raw_cfg, dict):
                _org_val = _raw_cfg.get("active_org")
                active_org = _org_val if isinstance(_org_val, str) else None
    except Exception as exc:
        logger.warning("⚠️ Could not read active_org from pipeline config: %s", exc)

    try:
        from agentception.readers.active_label_override import get_pin
        label_is_pinned = get_pin() is not None
    except Exception as exc:
        logger.warning("⚠️ Could not read active label pin: %s", exc)

    # Fetch these three concurrently — they're independent reads.
    scaling_advice: ScalingRecommendation | None = None
    pr_violations: list[PRViolation] = []
    from pathlib import Path as _Path
    poller_paused: bool = (_settings.ac_dir / ".pipeline-pause").exists()

    try:
        waves = await aggregate_waves()
        scaling_advice = await compute_recommendation(state, waves)
    except Exception as exc:
        logger.warning("⚠️ Could not compute scaling advice for SSR: %s", exc)

    try:
        pr_violations = await detect_out_of_order_prs()
    except Exception as exc:
        logger.warning("⚠️ Could not detect PR violations for SSR: %s", exc)

    board_issues = state.board_issues
    unclaimed = [i for i in board_issues if not i.claimed]

    # Phase gate lanes — pure computation, no I/O.
    phase_lanes: list[PhaseLane] = compute_phase_lanes(
        labels=all_phase_labels,
        board_issues=board_issues,
        agents=state.agents,
    )

    # Group board issues by phase_label for the batch-grouped board layout.
    # Issues without a phase_label are collected under "unassigned".
    board_issues_dicts = [i.model_dump() for i in board_issues]
    sorted_for_grouping = sorted(
        board_issues_dicts,
        key=lambda i: i.get("phase_label") or "unassigned",
    )
    grouped_board_issues: list[tuple[str, list[dict[str, object]]]] = [
        (batch_key, list(batch))
        for batch_key, batch in _groupby(
            sorted_for_grouping,
            key=lambda i: i.get("phase_label") or "unassigned",
        )
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "overview.html",
        {
            "state": state,
            "board_issues": board_issues_dicts,
            "grouped_board_issues": grouped_board_issues,
            "active_phase_label": state.active_label,
            "all_phase_labels": all_phase_labels,
            "label_is_pinned": label_is_pinned,
            "total_phase_issues": len(board_issues),
            "unclaimed_count": len(unclaimed),
            # Server-rendered to eliminate client-side fetch flicker.
            "scaling_advice": scaling_advice.model_dump() if scaling_advice else None,
            "pr_violations": [v.model_dump() for v in pr_violations],
            "poller_paused": poller_paused,
            "phase_lanes": phase_lanes,
            "active_org": active_org,
        },
    )


@router.post("/api/analyze/issue/{number}/partial", response_class=HTMLResponse)
async def analyze_partial(request: Request, number: int) -> HTMLResponse:
    """Return an HTMX partial with analysis results for a single GitHub issue.

    Calls :func:`~agentception.intelligence.analyzer.analyze_issue` with the
    given issue number, then renders ``partials/analysis.html`` with the
    :class:`~agentception.intelligence.analyzer.IssueAnalysis` result.

    Intended to be called by the "Analyze" button on each issue card in the
    GitHub board sidebar via ``hx-post`` / ``hx-swap="innerHTML"``.

    Parameters
    ----------
    number:
        GitHub issue number to analyse.

    Raises
    ------
    HTTP 404
        When the GitHub CLI cannot find the issue.
    HTTP 500
        When the ``gh`` subprocess fails for any other reason.
    """
    try:
        analysis: IssueAnalysis = await analyze_issue(number)
    except RuntimeError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 500
        raise HTTPException(status_code=status, detail=detail) from exc
    logger.info("✅ Analysis complete for issue #%d: %s", number, analysis.parallelism)
    return _TEMPLATES.TemplateResponse(
        request,
        "partials/analysis.html",
        {"a": analysis},
    )
