from __future__ import annotations

"""Smoke tests for agentception.db.queries additions in issue #858.

Full behavioural coverage is deferred to issue #859.  These tests verify
that the new symbols are importable and that the pure-Python derived-metric
logic (zero-denominator guards, cost constants) is correct without requiring
a live database connection.
"""

import datetime

import pytest

from agentception.db.queries import (
    DailyMetrics,
    _COST_CACHE_READ_PER_MTOK,
    _COST_CACHE_WRITE_PER_MTOK,
    _COST_INPUT_PER_MTOK,
    _COST_OUTPUT_PER_MTOK,
    get_daily_metrics,
)


def test_daily_metrics_importable() -> None:
    """DailyMetrics TypedDict and get_daily_metrics are importable."""
    assert DailyMetrics is not None
    assert callable(get_daily_metrics)


def test_cost_constants_match_sonnet_pricing() -> None:
    """Cost constants must match Claude Sonnet 4.6 pricing."""
    assert _COST_INPUT_PER_MTOK == pytest.approx(3.00)
    assert _COST_OUTPUT_PER_MTOK == pytest.approx(15.00)
    assert _COST_CACHE_WRITE_PER_MTOK == pytest.approx(3.75)
    assert _COST_CACHE_READ_PER_MTOK == pytest.approx(0.30)


def test_daily_metrics_is_typed_dict() -> None:
    """DailyMetrics must be constructable as a plain dict with the right keys."""
    m: DailyMetrics = {
        "date": "2025-01-01",
        "issues_closed": 0,
        "prs_merged": 0,
        "reviewer_runs": 0,
        "grade_a_count": 0,
        "grade_b_count": 0,
        "grade_c_count": 0,
        "grade_d_count": 0,
        "grade_f_count": 0,
        "first_pass_rate": 0.0,
        "rework_rate": 0.0,
        "avg_iterations": 0.0,
        "max_iter_hit_count": 0,
        "avg_cycle_time_seconds": 0.0,
        "cost_usd": 0.0,
        "cost_per_issue_usd": 0.0,
        "redispatch_count": 0,
        "auto_merge_rate": 0.0,
    }
    assert m["date"] == "2025-01-01"
    assert m["first_pass_rate"] == 0.0
    assert m["auto_merge_rate"] == 0.0


@pytest.mark.anyio
async def test_get_daily_metrics_returns_zeros_on_db_error() -> None:
    """get_daily_metrics must return a zero-filled DailyMetrics when the DB is unavailable."""
    from unittest.mock import AsyncMock, patch

    today = datetime.date(2025, 1, 15)

    # Patch get_session to raise so the except branch is exercised.
    with patch(
        "agentception.db.queries.get_session",
        side_effect=Exception("no db"),
    ):
        result = await get_daily_metrics(today)

    assert result["date"] == "2025-01-15"
    assert result["issues_closed"] == 0
    assert result["prs_merged"] == 0
    assert result["reviewer_runs"] == 0
    assert result["first_pass_rate"] == 0.0
    assert result["rework_rate"] == 0.0
    assert result["avg_iterations"] == 0.0
    assert result["cost_usd"] == 0.0
    assert result["cost_per_issue_usd"] == 0.0
    assert result["redispatch_count"] == 0
    assert result["auto_merge_rate"] == 0.0
