from __future__ import annotations

"""Tests for GET /api/health/detailed endpoint.

Tests cover:
- 200 status code returned.
- Response body matches HealthSnapshot schema (all 4 fields present).
- Each field is the correct type.
- health_collector.collect() is the sole delegate (no business logic in route).

Run targeted:
    pytest agentception/tests/test_health_endpoint.py -v
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models.health import HealthSnapshot

_MOCK_SNAPSHOT = HealthSnapshot(
    uptime_seconds=42.0,
    memory_rss_mb=128.5,
    active_worktree_count=3,
    github_api_latency_ms=55.2,
)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with lifespan handled."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def mock_collect() -> Generator[AsyncMock, None, None]:
    """Patch health_collector.collect for all tests in this module."""
    with patch(
        "agentception.routes.api.health.health_collector.collect",
        new_callable=AsyncMock,
        return_value=_MOCK_SNAPSHOT,
    ) as mock:
        yield mock


# ── Status code ───────────────────────────────────────────────────────────────


def test_health_detailed_returns_200(client: TestClient) -> None:
    """GET /api/health/detailed must return HTTP 200."""
    response = client.get("/api/health/detailed")
    assert response.status_code == 200


# ── Schema shape ──────────────────────────────────────────────────────────────


def test_health_detailed_response_has_all_four_fields(client: TestClient) -> None:
    """Response body must contain all four HealthSnapshot fields."""
    body = client.get("/api/health/detailed").json()
    assert "uptime_seconds" in body
    assert "memory_rss_mb" in body
    assert "active_worktree_count" in body
    assert "github_api_latency_ms" in body


# ── Field types ───────────────────────────────────────────────────────────────


def test_health_detailed_uptime_seconds_is_float(client: TestClient) -> None:
    body = client.get("/api/health/detailed").json()
    assert isinstance(body["uptime_seconds"], float)


def test_health_detailed_memory_rss_mb_is_float(client: TestClient) -> None:
    body = client.get("/api/health/detailed").json()
    assert isinstance(body["memory_rss_mb"], float)


def test_health_detailed_active_worktree_count_is_int(client: TestClient) -> None:
    body = client.get("/api/health/detailed").json()
    assert isinstance(body["active_worktree_count"], int)


def test_health_detailed_github_api_latency_ms_is_float(client: TestClient) -> None:
    body = client.get("/api/health/detailed").json()
    assert isinstance(body["github_api_latency_ms"], float)


# ── Delegation ────────────────────────────────────────────────────────────────


def test_health_detailed_delegates_to_collect(
    client: TestClient, mock_collect: AsyncMock
) -> None:
    """Route must call health_collector.collect() exactly once per request."""
    client.get("/api/health/detailed")
    mock_collect.assert_awaited_once()


def test_health_detailed_returns_collect_values(client: TestClient) -> None:
    """Values in the response must match what collect() returned."""
    body = client.get("/api/health/detailed").json()
    assert body["uptime_seconds"] == _MOCK_SNAPSHOT.uptime_seconds
    assert body["memory_rss_mb"] == _MOCK_SNAPSHOT.memory_rss_mb
    assert body["active_worktree_count"] == _MOCK_SNAPSHOT.active_worktree_count
    assert body["github_api_latency_ms"] == _MOCK_SNAPSHOT.github_api_latency_ms
