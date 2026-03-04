"""Intelligence-layer API routes for the AgentCeption dashboard.

Provides action endpoints for anomalies detected by
:mod:`agentception.intelligence.guards`.  The "clear stale claim" endpoint
is the primary consumer: the dashboard surfaces a "Clear Label" button for
each stale claim and POSTs here to remove the ``agent:wip`` label.

The ``POST /api/intelligence/scaling-advice/apply`` endpoint applies the
current scaling recommendation to ``pipeline-config.json`` in a single click,
allowing operators to act on advisor output without manual file edits.

Why a dedicated router?
- Keeps destructive write operations (label removal) separate from read-only
  data routes so they can be rate-limited or gated independently.
- ``/api/intelligence/`` signals to callers that these endpoints act on
  machine-detected anomalies rather than direct user operations.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agentception.intelligence.dag import DependencyDAG, build_dag
from agentception.intelligence.scaling import ScalingRecommendation, compute_recommendation
from agentception.poller import get_state
from agentception.models import PipelineConfig, PipelineState
from agentception.readers.github import clear_wip_label
from agentception.readers.pipeline_config import read_pipeline_config, write_pipeline_config
from agentception.telemetry import aggregate_waves

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


@router.get("/dag")
async def dag_api() -> DependencyDAG:
    """Return the full dependency DAG for all open issues.

    Fetches every open issue via the GitHub CLI, parses ``Depends on #N``
    declarations from each body, and returns a directed graph of dependencies.

    The response is consumed by the AC-402 DAG visualisation and by the
    intelligence layer's scheduling logic to prevent early assignment.

    Raises
    ------
    HTTP 500
        When the GitHub CLI subprocess fails (e.g. auth error, rate-limit).
    """
    try:
        return await build_dag()
    except RuntimeError as exc:
        logger.error("❌ Failed to build dependency DAG: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build dependency DAG: {exc}",
        ) from exc


@router.post("/stale-claims/{issue_number}/clear")
async def clear_stale_claim(issue_number: int) -> dict[str, int]:
    """Remove the ``agent:wip`` label from a stale-claim issue.

    Intended to be called from the dashboard "Clear Label" button when the
    poller detects that an issue carries ``agent:wip`` but has no live worktree.
    After clearing the label the issue re-enters the scheduling pool and the
    next polling tick will stop reporting it as a stale claim.

    Parameters
    ----------
    issue_number:
        GitHub issue number whose ``agent:wip`` label should be removed.

    Returns
    -------
    dict
        ``{"cleared": issue_number}`` on success.

    Raises
    ------
    HTTP 500
        When the ``gh`` CLI command fails (e.g. auth error or rate-limit).
    """
    try:
        await clear_wip_label(issue_number)
    except RuntimeError as exc:
        logger.error("❌ Failed to clear agent:wip from issue #%d: %s", issue_number, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear agent:wip label from issue #{issue_number}: {exc}",
        ) from exc

    logger.info("✅ Cleared stale claim: removed agent:wip from issue #%d", issue_number)
    return {"cleared": issue_number}


@router.post("/scaling-advice/apply")
async def apply_scaling_advice() -> dict[str, object]:
    """Apply the current scaling recommendation to ``pipeline-config.json``.

    Re-computes the recommendation from live pipeline state, then writes the
    recommended value to the appropriate ``PipelineConfig`` field.  When the
    recommendation is ``no_change`` the config is left untouched and the
    response reflects that.

    This is the backend for the one-click "Apply" button on the overview
    dashboard banner.  The endpoint is idempotent: calling it twice in a row
    will produce a second recommendation that may differ (e.g. if the first
    application already satisfied the threshold) and apply that too.

    Returns
    -------
    dict
        ``{"applied": action, "new_value": recommended_value}`` where
        ``action`` matches ``ScalingRecommendation.action`` and ``new_value``
        is the integer written to the config (0 when ``no_change``).

    Raises
    ------
    HTTP 500
        When wave aggregation, config I/O, or the recommendation engine fail.
    """
    try:
        state = get_state() or PipelineState.empty()
        waves = await aggregate_waves()
        rec = await compute_recommendation(state, waves)

        if rec.action == "no_change":
            logger.info("✅ apply_scaling_advice: no_change — config unchanged")
            return {"applied": rec.action, "new_value": 0}

        config = await read_pipeline_config()

        # Mutate only the field that the recommendation targets.
        new_config: PipelineConfig
        if rec.action == "increase_qa_vps":
            new_config = config.model_copy(update={"max_qa_vps": rec.recommended_value})
        elif rec.action == "increase_pool":
            new_config = config.model_copy(update={"pool_size_per_vp": rec.recommended_value})
        elif rec.action == "increase_eng_vps":
            new_config = config.model_copy(update={"max_eng_vps": rec.recommended_value})
        # All Literal branches handled above; mypy may still flag the else as unreachable.
        # The dead-code guard is kept for runtime safety when called with coerced types.

        await write_pipeline_config(new_config)
        logger.info(
            "✅ apply_scaling_advice: %s → %d (was %d)",
            rec.action,
            rec.recommended_value,
            rec.current_value,
        )
        return {"applied": rec.action, "new_value": rec.recommended_value}

    except Exception as exc:
        logger.error("❌ Failed to apply scaling advice: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply scaling advice: {exc}",
        ) from exc


@router.get("/scaling-advice")
async def scaling_advice_api() -> ScalingRecommendation:
    """Return a scaling recommendation based on current pipeline state and wave history.

    Evaluates queue depth (open issues), PR backlog (open PRs), and mean wave
    duration to produce an actionable recommendation.  Uses the current
    ``PipelineState`` snapshot from the poller and historical wave data from
    the telemetry layer — no GitHub API calls are made during this request.

    Returns
    -------
    ScalingRecommendation
        The recommended action (``increase_qa_vps``, ``increase_pool``, or
        ``no_change``) along with rationale and confidence level.

    Raises
    ------
    HTTP 500
        When wave aggregation or config reads fail unexpectedly.
    """
    try:
        state = get_state() or PipelineState.empty()
        waves = await aggregate_waves()
        return await compute_recommendation(state, waves)
    except Exception as exc:
        logger.error("❌ Failed to compute scaling recommendation: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to compute scaling recommendation: {exc}",
        ) from exc
