"""API routes: telemetry waves and cost aggregates."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from agentception.telemetry import WaveSummary, aggregate_waves

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/telemetry/waves", tags=["telemetry"])
async def waves_api() -> list[WaveSummary]:
    """Return a list of WaveSummary objects, one per unique BATCH_ID.

    Scans all active ``.agent-task`` files in the worktrees directory, groups
    them by their ``BATCH_ID`` field, and computes timing from file mtimes.
    Returns an empty list when no worktrees are present or none carry a
    ``BATCH_ID``.  Results are sorted most-recent-first by ``started_at``.
    """
    return await aggregate_waves()


@router.get("/telemetry/cost", tags=["telemetry"])
async def total_cost_api() -> dict[str, float | int]:
    """Return the aggregate token and cost estimate across all historical waves.

    Sums ``estimated_tokens`` and ``estimated_cost_usd`` from every wave
    returned by ``aggregate_waves()``.  The result is a stable summary
    useful for dashboards and budget tracking without iterating wave data
    on the client side.

    Returns ``{"total_tokens": int, "total_cost_usd": float, "wave_count": int}``.
    """
    waves = await aggregate_waves()
    total_tokens = sum(w.estimated_tokens for w in waves)
    total_cost_usd = round(sum(w.estimated_cost_usd for w in waves), 4)
    return {
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "wave_count": len(waves),
    }
