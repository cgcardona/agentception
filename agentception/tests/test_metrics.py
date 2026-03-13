"""Tests for GET /api/metrics/daily and GET /api/metrics/daily/range.

All tests mock ``get_daily_metrics`` so no real DB connection is required.
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.db.queries import DailyMetrics
from agentception.routes.api.metrics import DailyMetricsResponse

client = TestClient(app)
_MOCK_PATH = "agentception.routes.api.metrics.get_daily_metrics"


def _zero_metrics(date: str = "2025-01-15") -> DailyMetrics:
    """Return a DailyMetrics TypedDict with every numeric field zeroed."""
    return DailyMetrics(
        date=date,
        issues_closed=0,
        prs_merged=0,
        reviewer_runs=0,
        grade_a_count=0,
        grade_b_count=0,
        grade_c_count=0,
        grade_d_count=0,
        grade_f_count=0,
        first_pass_rate=0.0,
        rework_rate=0.0,
        avg_iterations=0.0,
        max_iter_hit_count=0,
        avg_cycle_time_seconds=0.0,
        cost_usd=0.0,
        cost_per_issue_usd=0.0,
        redispatch_count=0,
        auto_merge_rate=0.0,
    )


def test_daily_metrics_empty_day() -> None:
    """GET /api/metrics/daily?date=2025-01-15 returns 200 with all-zero fields."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=_zero_metrics())):
        response = client.get("/api/metrics/daily?date=2025-01-15")

    assert response.status_code == 200
    data = response.json()

    int_fields = [
        "issues_closed",
        "prs_merged",
        "reviewer_runs",
        "grade_a_count",
        "grade_b_count",
        "grade_c_count",
        "grade_d_count",
        "grade_f_count",
        "max_iter_hit_count",
        "redispatch_count",
    ]
    float_fields = [
        "first_pass_rate",
        "rework_rate",
        "avg_iterations",
        "avg_cycle_time_seconds",
        "cost_usd",
        "cost_per_issue_usd",
        "auto_merge_rate",
    ]

    for field in int_fields:
        assert data[field] == 0, f"expected {field} == 0, got {data[field]}"
    for field in float_fields:
        assert data[field] == 0.0, f"expected {field} == 0.0, got {data[field]}"


def test_daily_metrics_grade_distribution() -> None:
    """GET /api/metrics/daily returns correct grade counts and first_pass_rate."""
    metrics = _zero_metrics()
    metrics["grade_a_count"] = 2
    metrics["grade_b_count"] = 1
    metrics["reviewer_runs"] = 3
    metrics["first_pass_rate"] = 1.0

    with patch(_MOCK_PATH, new=AsyncMock(return_value=metrics)):
        response = client.get("/api/metrics/daily?date=2025-01-15")

    assert response.status_code == 200
    data = response.json()
    assert data["grade_a_count"] == 2
    assert data["grade_b_count"] == 1
    assert data["first_pass_rate"] == pytest.approx(1.0)


def test_daily_metrics_cost_calculation() -> None:
    """GET /api/metrics/daily returns the correct cost_usd value."""
    metrics = _zero_metrics()
    metrics["cost_usd"] = 18.0

    with patch(_MOCK_PATH, new=AsyncMock(return_value=metrics)):
        response = client.get("/api/metrics/daily?date=2025-01-15")

    assert response.status_code == 200
    assert response.json()["cost_usd"] == pytest.approx(18.0)


def test_daily_metrics_rework_rate() -> None:
    """GET /api/metrics/daily returns the correct rework_rate value."""
    metrics = _zero_metrics()
    metrics["rework_rate"] = 0.5

    with patch(_MOCK_PATH, new=AsyncMock(return_value=metrics)):
        response = client.get("/api/metrics/daily?date=2025-01-15")

    assert response.status_code == 200
    assert response.json()["rework_rate"] == pytest.approx(0.5)


def test_metrics_api_endpoint_returns_today() -> None:
    """GET /api/metrics/daily (no params) returns 200 with all expected fields."""
    today = str(datetime.date.today())

    with patch(_MOCK_PATH, new=AsyncMock(return_value=_zero_metrics(date=today))):
        response = client.get("/api/metrics/daily")

    assert response.status_code == 200
    data = response.json()
    for field in DailyMetricsResponse.model_fields:
        assert field in data, f"expected field '{field}' in response JSON"


def test_metrics_api_range_endpoint() -> None:
    """GET /api/metrics/daily/range returns one entry per day, ascending."""
    mock = AsyncMock(
        side_effect=[
            _zero_metrics("2025-01-01"),
            _zero_metrics("2025-01-02"),
            _zero_metrics("2025-01-03"),
        ]
    )

    with patch(_MOCK_PATH, new=mock):
        response = client.get(
            "/api/metrics/daily/range?start=2025-01-01&end=2025-01-03"
        )

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 3
    assert items[0]["date"] == "2025-01-01"
    assert items[1]["date"] == "2025-01-02"
    assert items[2]["date"] == "2025-01-03"
