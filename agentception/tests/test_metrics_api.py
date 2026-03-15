from __future__ import annotations

"""Unit tests for GET /api/metrics/daily and GET /api/metrics/daily/range.

All tests mock ``agentception.db.queries.get_daily_metrics`` so no real DB
connection is required.
"""

import datetime
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.db.queries import DailyMetrics

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_METRICS_STUB: DailyMetrics = {
    "date": "2025-01-15",
    "issues_closed": 3,
    "prs_merged": 2,
    "reviewer_runs": 4,
    "grade_a_count": 2,
    "grade_b_count": 1,
    "grade_c_count": 1,
    "grade_d_count": 0,
    "grade_f_count": 0,
    "first_pass_rate": 0.75,
    "rework_rate": 0.1,
    "avg_iterations": 5.0,
    "max_iter_hit_count": 0,
    "avg_cycle_time_seconds": 120.0,
    "cost_usd": 0.42,
    "cost_per_issue_usd": 0.14,
    "redispatch_count": 1,
    "auto_merge_rate": 0.75,
}


def _make_stub(date_str: str) -> DailyMetrics:
    """Return a metrics stub with the given date string."""
    return {**_METRICS_STUB, "date": date_str}


@pytest.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Thin ASGI test client wrapping only the metrics router."""
    from fastapi import FastAPI

    from agentception.routes.api.metrics import router

    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /metrics/daily — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_metrics_daily_default_date(client: AsyncClient) -> None:
    """No date param → uses today, returns 200 with all DailyMetricsResponse fields."""
    with patch(
        "agentception.routes.api.metrics.get_daily_metrics",
        new_callable=AsyncMock,
        return_value=_METRICS_STUB,
    ):
        response = await client.get("/metrics/daily")

    assert response.status_code == 200
    data = response.json()
    assert data["date"] == "2025-01-15"
    assert data["issues_closed"] == 3
    assert data["prs_merged"] == 2
    assert data["reviewer_runs"] == 4
    assert data["grade_a_count"] == 2
    assert data["grade_b_count"] == 1
    assert data["grade_c_count"] == 1
    assert data["grade_d_count"] == 0
    assert data["grade_f_count"] == 0
    assert data["first_pass_rate"] == 0.75
    assert data["rework_rate"] == 0.1
    assert data["avg_iterations"] == 5.0
    assert data["max_iter_hit_count"] == 0
    assert data["avg_cycle_time_seconds"] == 120.0
    assert data["cost_usd"] == 0.42
    assert data["cost_per_issue_usd"] == 0.14
    assert data["redispatch_count"] == 1
    assert data["auto_merge_rate"] == 0.75


@pytest.mark.anyio
async def test_get_metrics_daily_explicit_date(client: AsyncClient) -> None:
    """date=2025-01-15 → returns 200."""
    stub = _make_stub("2025-01-15")
    with patch(
        "agentception.routes.api.metrics.get_daily_metrics",
        new_callable=AsyncMock,
        return_value=stub,
    ):
        response = await client.get("/metrics/daily", params={"date": "2025-01-15"})

    assert response.status_code == 200
    assert response.json()["date"] == "2025-01-15"


# ---------------------------------------------------------------------------
# GET /metrics/daily — error paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_metrics_daily_bad_date_returns_400(client: AsyncClient) -> None:
    """date=bad → 400."""
    response = await client.get("/metrics/daily", params={"date": "bad"})
    assert response.status_code == 400
    assert "bad" in response.json()["detail"]


@pytest.mark.anyio
async def test_get_metrics_daily_invalid_format_returns_400(client: AsyncClient) -> None:
    """date=2025/01/15 (wrong separator) → 400."""
    response = await client.get("/metrics/daily", params={"date": "2025/01/15"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /metrics/daily/range — happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_metrics_daily_range_three_days(client: AsyncClient) -> None:
    """start=2025-01-01&end=2025-01-03 → list of 3 objects, sorted ascending."""

    async def _fake_get_daily_metrics(date: datetime.date) -> DailyMetrics:
        import datetime

        assert isinstance(date, datetime.date)
        return _make_stub(date.isoformat())

    with patch(
        "agentception.routes.api.metrics.get_daily_metrics",
        side_effect=_fake_get_daily_metrics,
    ):
        response = await client.get(
            "/metrics/daily/range",
            params={"start": "2025-01-01", "end": "2025-01-03"},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert data[0]["date"] == "2025-01-01"
    assert data[1]["date"] == "2025-01-02"
    assert data[2]["date"] == "2025-01-03"


@pytest.mark.anyio
async def test_get_metrics_daily_range_single_day(client: AsyncClient) -> None:
    """start == end → list of 1 object."""

    async def _fake(date: datetime.date) -> DailyMetrics:
        import datetime

        assert isinstance(date, datetime.date)
        return _make_stub(date.isoformat())

    with patch(
        "agentception.routes.api.metrics.get_daily_metrics",
        side_effect=_fake,
    ):
        response = await client.get(
            "/metrics/daily/range",
            params={"start": "2025-06-01", "end": "2025-06-01"},
        )

    assert response.status_code == 200
    assert len(response.json()) == 1


# ---------------------------------------------------------------------------
# GET /metrics/daily/range — error paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_metrics_daily_range_end_before_start_returns_400(
    client: AsyncClient,
) -> None:
    """end < start → 400."""
    response = await client.get(
        "/metrics/daily/range",
        params={"start": "2025-01-10", "end": "2025-01-01"},
    )
    assert response.status_code == 400
    assert "end" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_get_metrics_daily_range_over_30_days_returns_422(
    client: AsyncClient,
) -> None:
    """Range > 30 days → 422."""
    response = await client.get(
        "/metrics/daily/range",
        params={"start": "2025-01-01", "end": "2025-02-15"},
    )
    assert response.status_code == 422
    assert "30" in response.json()["detail"]


@pytest.mark.anyio
async def test_get_metrics_daily_range_bad_start_returns_400(
    client: AsyncClient,
) -> None:
    """Malformed start date → 400."""
    response = await client.get(
        "/metrics/daily/range",
        params={"start": "not-a-date", "end": "2025-01-03"},
    )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_get_metrics_daily_range_bad_end_returns_400(
    client: AsyncClient,
) -> None:
    """Malformed end date → 400."""
    response = await client.get(
        "/metrics/daily/range",
        params={"start": "2025-01-01", "end": "nope"},
    )
    assert response.status_code == 400
