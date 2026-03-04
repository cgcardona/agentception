from __future__ import annotations

"""Tests for GET /ui/health/widget — the HTMX System Health card fragment.

Covers:
  - 200 response with HTML content-type
  - Rendered fragment contains the required CSS classes
  - Status dot is --nominal when thresholds are satisfied
  - Status dot is --elevated when latency threshold is exceeded
  - Status dot is --elevated when memory threshold is exceeded
  - Status dot is --elevated when latency sentinel (-1.0) is present
  - _fmt_uptime helper formats durations correctly
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.app import app
from agentception.models.health import HealthSnapshot
from agentception.routes.ui.health import _fmt_uptime

_NOMINAL_SNAPSHOT = HealthSnapshot(
    uptime_seconds=3725.0,
    memory_rss_mb=128.0,
    active_worktree_count=3,
    github_api_latency_ms=42.0,
)

_ELEVATED_LATENCY_SNAPSHOT = HealthSnapshot(
    uptime_seconds=60.0,
    memory_rss_mb=128.0,
    active_worktree_count=1,
    github_api_latency_ms=600.0,
)

_ELEVATED_MEMORY_SNAPSHOT = HealthSnapshot(
    uptime_seconds=60.0,
    memory_rss_mb=600.0,
    active_worktree_count=1,
    github_api_latency_ms=100.0,
)

_PROBE_FAILED_SNAPSHOT = HealthSnapshot(
    uptime_seconds=10.0,
    memory_rss_mb=64.0,
    active_worktree_count=0,
    github_api_latency_ms=-1.0,
)


@pytest.mark.anyio
async def test_health_widget_returns_200() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert response.status_code == 200


@pytest.mark.anyio
async def test_health_widget_returns_html_content_type() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "text/html" in response.headers.get("content-type", "")


@pytest.mark.anyio
async def test_health_widget_contains_health_card_class() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-card" in response.text


@pytest.mark.anyio
async def test_health_widget_contains_health_metric_class() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-metric" in response.text


@pytest.mark.anyio
async def test_health_widget_contains_health_status_dot() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-status-dot" in response.text


@pytest.mark.anyio
async def test_health_widget_nominal_dot_when_thresholds_satisfied() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_NOMINAL_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-status-dot--nominal" in response.text
    assert "health-status-dot--elevated" not in response.text


@pytest.mark.anyio
async def test_health_widget_elevated_dot_when_latency_exceeded() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_ELEVATED_LATENCY_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-status-dot--elevated" in response.text
    assert "health-status-dot--nominal" not in response.text


@pytest.mark.anyio
async def test_health_widget_elevated_dot_when_memory_exceeded() -> None:
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_ELEVATED_MEMORY_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-status-dot--elevated" in response.text


@pytest.mark.anyio
async def test_health_widget_elevated_dot_when_probe_failed() -> None:
    """Sentinel latency value (-1.0) must result in --elevated status dot."""
    with patch(
        "agentception.routes.ui.health.health_collector.collect",
        new=AsyncMock(return_value=_PROBE_FAILED_SNAPSHOT),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ui/health/widget")

    assert "health-status-dot--elevated" in response.text


# ---------------------------------------------------------------------------
# Unit tests for _fmt_uptime
# ---------------------------------------------------------------------------


def test_fmt_uptime_seconds_only() -> None:
    assert _fmt_uptime(45.0) == "45s"


def test_fmt_uptime_minutes_and_seconds() -> None:
    assert _fmt_uptime(125.0) == "2m 5s"


def test_fmt_uptime_hours_and_minutes() -> None:
    assert _fmt_uptime(3725.0) == "1h 2m"


def test_fmt_uptime_zero() -> None:
    assert _fmt_uptime(0.0) == "0s"
