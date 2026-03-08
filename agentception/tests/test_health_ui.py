"""Unit and integration tests for agentception.routes.ui.health.

Covers:

  _fmt_uptime()           — pure formatter for the uptime duration display
  GET /ui/health/widget   — HTMX fragment: 200, HTML content-type, is_nominal logic

``_fmt_uptime`` has three branches (seconds / minutes / hours) that are each
thoroughly exercised including exact boundary values.

The widget endpoint is tested for status code, content-type, and the
``is_nominal`` flag that drives the status-dot BEM modifier.  Template content
is intentionally not asserted verbatim to avoid brittle string matching.

Run targeted:
    pytest agentception/tests/test_health_ui.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models.health import HealthSnapshot
from agentception.routes.ui.health import _fmt_uptime


# ── _fmt_uptime ───────────────────────────────────────────────────────────────


class TestFmtUptime:
    """All branches and boundary conditions for _fmt_uptime."""

    # seconds branch (< 60)
    def test_zero_seconds(self) -> None:
        assert _fmt_uptime(0.0) == "0s"

    def test_one_second(self) -> None:
        assert _fmt_uptime(1.0) == "1s"

    def test_59_seconds(self) -> None:
        assert _fmt_uptime(59.9) == "59s"

    # minutes branch (60 ≤ seconds < 3600)
    def test_exactly_60_seconds(self) -> None:
        assert _fmt_uptime(60.0) == "1m 0s"

    def test_90_seconds(self) -> None:
        assert _fmt_uptime(90.0) == "1m 30s"

    def test_119_seconds(self) -> None:
        assert _fmt_uptime(119.0) == "1m 59s"

    def test_3599_seconds(self) -> None:
        assert _fmt_uptime(3599.0) == "59m 59s"

    # hours branch (≥ 3600)
    def test_exactly_one_hour(self) -> None:
        assert _fmt_uptime(3600.0) == "1h 0m"

    def test_one_hour_30_minutes(self) -> None:
        assert _fmt_uptime(5400.0) == "1h 30m"

    def test_two_hours_one_minute(self) -> None:
        assert _fmt_uptime(7260.0) == "2h 1m"

    def test_large_duration(self) -> None:
        # 25 h 0 m (90 000 s) — verify no integer overflow or format break
        assert _fmt_uptime(90_000.0) == "25h 0m"


# ── GET /ui/health/widget ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client to avoid repeated lifespan startup."""
    with TestClient(app) as c:
        yield c


_COLLECT_PATH = "agentception.routes.ui.health.health_collector.collect"


def test_health_widget_returns_200(client: TestClient) -> None:
    """GET /ui/health/widget must always return HTTP 200."""
    snapshot = HealthSnapshot(
        uptime_seconds=60.0, memory_rss_mb=128.0,
        active_worktree_count=1, github_api_latency_ms=80.0,
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert response.status_code == 200


def test_health_widget_returns_html_content_type(client: TestClient) -> None:
    """GET /ui/health/widget must return text/html content-type."""
    snapshot = HealthSnapshot(
        uptime_seconds=60.0, memory_rss_mb=128.0,
        active_worktree_count=1, github_api_latency_ms=80.0,
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert "text/html" in response.headers["content-type"]


def test_health_widget_is_nominal_when_all_thresholds_ok(client: TestClient) -> None:
    """Widget body must include 'nominal' state indicator when latency and memory are within limits."""
    snapshot = HealthSnapshot(
        uptime_seconds=120.0,
        memory_rss_mb=256.0,        # < 512 MB threshold
        active_worktree_count=2,
        github_api_latency_ms=200.0,  # < 500 ms threshold
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert "nominal" in response.text


def test_health_widget_is_elevated_when_probe_failed(client: TestClient) -> None:
    """Widget body must include 'elevated' state indicator when the GitHub probe returned -1.0."""
    snapshot = HealthSnapshot(
        uptime_seconds=5.0, memory_rss_mb=128.0,
        active_worktree_count=0,
        github_api_latency_ms=-1.0,  # probe failure sentinel
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert "elevated" in response.text


def test_health_widget_is_elevated_when_memory_exceeds_threshold(client: TestClient) -> None:
    """Widget body must include 'elevated' when memory_rss_mb >= 512."""
    snapshot = HealthSnapshot(
        uptime_seconds=200.0,
        memory_rss_mb=513.0,        # over threshold
        active_worktree_count=0, github_api_latency_ms=50.0,
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert "elevated" in response.text


def test_health_widget_is_elevated_when_latency_exceeds_threshold(client: TestClient) -> None:
    """Widget body must include 'elevated' when github_api_latency_ms >= 500."""
    snapshot = HealthSnapshot(
        uptime_seconds=300.0, memory_rss_mb=100.0,
        active_worktree_count=1,
        github_api_latency_ms=600.0,  # over threshold
    )
    with patch(_COLLECT_PATH, new_callable=AsyncMock, return_value=snapshot):
        response = client.get("/ui/health/widget")
    assert "elevated" in response.text
