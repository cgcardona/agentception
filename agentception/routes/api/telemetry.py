"""API routes: telemetry waves and cost aggregates."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from agentception.telemetry import WaveSummary, aggregate_waves

router = APIRouter()


class TelemetryCostSummary(BaseModel):
    """Aggregate token and cost summary across all historical waves."""

    total_tokens: int
    total_cost_usd: float
    wave_count: int


@router.get("/telemetry/waves", tags=["telemetry"])
async def waves_api() -> list[WaveSummary]:
    """Return a list of WaveSummary objects, one per unique BATCH_ID.

    Scans all active worktrees, groups
    them by their ``BATCH_ID`` field, and computes timing from file mtimes.
    Returns an empty list when no worktrees are present or none carry a
    ``BATCH_ID``.  Results are sorted most-recent-first by ``started_at``.
    """
    return await aggregate_waves()


@router.get("/telemetry/cost", tags=["telemetry"])
async def total_cost_api() -> TelemetryCostSummary:
    """Return the aggregate token and cost estimate across all historical waves.

    Sums ``estimated_tokens`` and ``estimated_cost_usd`` from every wave
    returned by ``aggregate_waves()``.  The result is a stable summary
    useful for dashboards and budget tracking without iterating wave data
    on the client side.
    """
    waves = await aggregate_waves()
    return TelemetryCostSummary(
        total_tokens=sum(w.estimated_tokens for w in waves),
        total_cost_usd=round(sum(w.estimated_cost_usd for w in waves), 4),
        wave_count=len(waves),
    )
