"""Tests for agentception/routes/api/telemetry.py.

Covers both JSON API endpoints:

    GET /api/telemetry/waves  — list of WaveSummary objects
    GET /api/telemetry/cost   — TelemetryCostSummary aggregate

All calls to aggregate_waves are mocked so no filesystem or DB I/O occurs.

Run targeted:
    pytest agentception/tests/test_telemetry_api.py -v
"""
from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.telemetry import WaveSummary


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


def _wave(
    batch_id: str,
    *,
    tokens: int = 0,
    cost: float = 0.0,
    issues: list[int] | None = None,
) -> WaveSummary:
    """Build a minimal WaveSummary for test assertions."""
    return WaveSummary(
        batch_id=batch_id,
        started_at=time.time(),
        ended_at=None,
        issues_worked=issues or [],
        prs_opened=0,
        prs_merged=0,
        estimated_tokens=tokens,
        estimated_cost_usd=cost,
        agents=[],
    )


_MOCK_PATH = "agentception.routes.api.telemetry.aggregate_waves"


# ── GET /api/telemetry/waves ──────────────────────────────────────────────────


def test_waves_returns_200(client: TestClient) -> None:
    """GET /api/telemetry/waves returns HTTP 200."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=[])):
        response = client.get("/api/telemetry/waves")
    assert response.status_code == 200


def test_waves_returns_empty_list_when_no_waves(client: TestClient) -> None:
    """GET /api/telemetry/waves returns [] when aggregate_waves returns no data."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=[])):
        response = client.get("/api/telemetry/waves")
    assert response.json() == []


def test_waves_returns_all_waves(client: TestClient) -> None:
    """GET /api/telemetry/waves returns one item per wave from aggregate_waves."""
    waves = [_wave("batch-A"), _wave("batch-B")]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/waves")
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2


def test_waves_items_have_batch_id(client: TestClient) -> None:
    """GET /api/telemetry/waves items include the batch_id field."""
    waves = [_wave("eng-batch-X", issues=[42, 43])]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/waves")
    body = response.json()
    assert body[0]["batch_id"] == "eng-batch-X"


def test_waves_items_have_required_fields(client: TestClient) -> None:
    """GET /api/telemetry/waves items include all WaveSummary fields."""
    required = {
        "batch_id", "started_at", "ended_at",
        "issues_worked", "prs_opened", "prs_merged",
        "estimated_tokens", "estimated_cost_usd",
    }
    waves = [_wave("batch-fields", tokens=1000, cost=0.012, issues=[10])]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/waves")
    body = response.json()
    assert required.issubset(body[0].keys())


def test_waves_preserves_issues_worked(client: TestClient) -> None:
    """GET /api/telemetry/waves preserves the issues_worked list from each wave."""
    waves = [_wave("batch-issues", issues=[101, 202, 303])]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/waves")
    assert response.json()[0]["issues_worked"] == [101, 202, 303]


# ── GET /api/telemetry/cost ───────────────────────────────────────────────────


def test_cost_returns_200(client: TestClient) -> None:
    """GET /api/telemetry/cost returns HTTP 200."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=[])):
        response = client.get("/api/telemetry/cost")
    assert response.status_code == 200


def test_cost_returns_zeros_when_no_waves(client: TestClient) -> None:
    """GET /api/telemetry/cost returns zero-value summary when there are no waves."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=[])):
        response = client.get("/api/telemetry/cost")
    body = response.json()
    assert body["total_tokens"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["wave_count"] == 0


def test_cost_has_expected_fields(client: TestClient) -> None:
    """GET /api/telemetry/cost response includes total_tokens, total_cost_usd, wave_count."""
    with patch(_MOCK_PATH, new=AsyncMock(return_value=[])):
        response = client.get("/api/telemetry/cost")
    body = response.json()
    assert "total_tokens" in body
    assert "total_cost_usd" in body
    assert "wave_count" in body


def test_cost_sums_tokens_across_waves(client: TestClient) -> None:
    """GET /api/telemetry/cost sums estimated_tokens from all waves."""
    waves = [
        _wave("batch-1", tokens=50_000, cost=0.5),
        _wave("batch-2", tokens=30_000, cost=0.3),
    ]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/cost")
    assert response.json()["total_tokens"] == 80_000


def test_cost_sums_cost_usd_across_waves(client: TestClient) -> None:
    """GET /api/telemetry/cost sums estimated_cost_usd from all waves (rounded to 4dp)."""
    waves = [
        _wave("batch-1", tokens=0, cost=1.2345),
        _wave("batch-2", tokens=0, cost=0.6789),
    ]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/cost")
    body = response.json()
    assert body["total_cost_usd"] == pytest.approx(round(1.2345 + 0.6789, 4), abs=1e-4)


def test_cost_wave_count_equals_number_of_waves(client: TestClient) -> None:
    """GET /api/telemetry/cost wave_count matches the number of waves returned."""
    waves = [_wave("batch-A"), _wave("batch-B"), _wave("batch-C")]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/cost")
    assert response.json()["wave_count"] == 3


def test_cost_single_wave(client: TestClient) -> None:
    """GET /api/telemetry/cost with a single wave returns that wave's exact values."""
    waves = [_wave("batch-solo", tokens=12_500, cost=0.1875)]
    with patch(_MOCK_PATH, new=AsyncMock(return_value=waves)):
        response = client.get("/api/telemetry/cost")
    body = response.json()
    assert body["total_tokens"] == 12_500
    assert body["total_cost_usd"] == pytest.approx(0.1875, abs=1e-4)
    assert body["wave_count"] == 1
