"""API routes: GET /api/metrics/daily and GET /api/metrics/daily/range.

Thin handlers — all DB logic lives in ``agentception.db.queries.get_daily_metrics``.
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from agentception.db.queries import DailyMetrics, get_daily_metrics

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


class DailyMetricsResponse(BaseModel):
    """Serialisable daily KPI snapshot returned by the metrics endpoints."""

    date: str
    issues_closed: int
    prs_merged: int
    reviewer_runs: int
    grade_a_count: int
    grade_b_count: int
    grade_c_count: int
    grade_d_count: int
    grade_f_count: int
    first_pass_rate: float
    rework_rate: float
    avg_iterations: float
    max_iter_hit_count: int
    avg_cycle_time_seconds: float
    cost_usd: float
    cost_per_issue_usd: float
    redispatch_count: int
    auto_merge_rate: float

    @classmethod
    def from_daily_metrics(cls, m: DailyMetrics) -> DailyMetricsResponse:
        """Construct a response model from a DailyMetrics TypedDict."""
        return cls(**m)


@router.get("/metrics/daily", response_model=DailyMetricsResponse)
async def get_metrics_daily(
    date: str | None = None,
) -> DailyMetricsResponse:
    """Return the KPI snapshot for a single calendar day.

    Query parameter ``date`` must be an ISO date string (``YYYY-MM-DD``).
    Defaults to today (UTC) when omitted.
    """
    if date is None:
        target = datetime.date.today()
    else:
        target = datetime.date.fromisoformat(date)
    metrics: DailyMetrics = await get_daily_metrics(target)
    return DailyMetricsResponse.from_daily_metrics(metrics)


@router.get("/metrics/daily/range", response_model=list[DailyMetricsResponse])
async def get_metrics_daily_range(
    start: str,
    end: str,
) -> list[DailyMetricsResponse]:
    """Return KPI snapshots for every day in the inclusive range [start, end].

    Both ``start`` and ``end`` must be ISO date strings (``YYYY-MM-DD``).
    Results are returned in ascending date order.
    """
    start_date = datetime.date.fromisoformat(start)
    end_date = datetime.date.fromisoformat(end)

    results: list[DailyMetricsResponse] = []
    current = start_date
    while current <= end_date:
        metrics: DailyMetrics = await get_daily_metrics(current)
        results.append(DailyMetricsResponse.from_daily_metrics(metrics))
        current += datetime.timedelta(days=1)
    return results
