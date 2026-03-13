from __future__ import annotations

"""Metrics API endpoints — daily KPI snapshots.

Exposes ``get_daily_metrics()`` from ``agentception.db.queries`` over HTTP so
dashboards and external tooling can consume daily performance data without
direct DB access.
"""

import datetime
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from agentception.db.queries import get_daily_metrics

logger = logging.getLogger(__name__)

router = APIRouter()


class DailyMetricsResponse(BaseModel):
    """KPI snapshot for a single calendar day."""

    model_config = ConfigDict(frozen=True)

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


def _parse_iso_date(date_str: str) -> datetime.date:
    """Parse an ISO-8601 date string, raising HTTP 400 on failure."""
    try:
        return datetime.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date '{date_str}'. Expected ISO format YYYY-MM-DD.",
        )


@router.get("/metrics/daily", response_model=DailyMetricsResponse)
async def get_metrics_daily(
    date: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to today."),
) -> DailyMetricsResponse:
    """Return the KPI snapshot for a single calendar day.

    When *date* is omitted the current UTC date is used.
    """
    if date is None:
        target = datetime.date.today()
    else:
        target = _parse_iso_date(date)

    metrics = await get_daily_metrics(target)
    return DailyMetricsResponse(**metrics)


@router.get("/metrics/daily/range", response_model=list[DailyMetricsResponse])
async def get_metrics_daily_range(
    start: str = Query(description="Start date (YYYY-MM-DD), inclusive."),
    end: str = Query(description="End date (YYYY-MM-DD), inclusive."),
) -> list[DailyMetricsResponse]:
    """Return KPI snapshots for every day in [start, end], sorted ascending.

    Returns HTTP 400 when end < start or either date is malformed.
    Returns HTTP 422 when the range exceeds 30 days.
    """
    start_date = _parse_iso_date(start)
    end_date = _parse_iso_date(end)

    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail=f"end ({end}) must not be before start ({start}).",
        )

    span = (end_date - start_date).days
    if span > 30:
        raise HTTPException(
            status_code=422,
            detail=f"Range of {span} days exceeds the 30-day maximum.",
        )

    results: list[DailyMetricsResponse] = []
    current = start_date
    while current <= end_date:
        metrics = await get_daily_metrics(current)
        results.append(DailyMetricsResponse(**metrics))
        current += datetime.timedelta(days=1)

    return results
