from __future__ import annotations

"""Tests for the health_collector async service."""

import time

import pytest

from agentception.models.health import HealthSnapshot
from agentception.services import health_collector


def _reset_cache() -> None:
    """Reset module-level cache state for test isolation."""
    health_collector._cached = None
    health_collector._cached_at = 0.0


@pytest.mark.anyio
async def test_collect_returns_health_snapshot() -> None:
    _reset_cache()
    snap = await health_collector.collect()
    assert isinstance(snap, HealthSnapshot)


@pytest.mark.anyio
async def test_collect_uptime_is_positive() -> None:
    _reset_cache()
    snap = await health_collector.collect()
    assert snap.uptime_seconds > 0.0


@pytest.mark.anyio
async def test_collect_memory_is_positive() -> None:
    _reset_cache()
    snap = await health_collector.collect()
    assert snap.memory_rss_mb > 0.0


@pytest.mark.anyio
async def test_collect_worktree_count_is_non_negative() -> None:
    _reset_cache()
    snap = await health_collector.collect()
    assert snap.active_worktree_count >= 0


@pytest.mark.anyio
async def test_collect_github_latency_is_valid_value() -> None:
    """Latency must be -1.0 (sentinel) or a positive number."""
    _reset_cache()
    snap = await health_collector.collect()
    assert snap.github_api_latency_ms == -1.0 or snap.github_api_latency_ms > 0.0


@pytest.mark.anyio
async def test_collect_result_is_cached() -> None:
    """Two rapid calls must return the identical object (cache hit)."""
    _reset_cache()
    snap1 = await health_collector.collect()
    snap2 = await health_collector.collect()
    assert snap1 is snap2


@pytest.mark.anyio
async def test_collect_cache_expires_after_ttl() -> None:
    """After the TTL elapses the collector re-gathers and returns a new object."""
    _reset_cache()
    snap1 = await health_collector.collect()
    # Manually expire the cache
    health_collector._cached_at = time.monotonic() - health_collector._CACHE_TTL_SECONDS - 1.0
    snap2 = await health_collector.collect()
    assert snap2 is not snap1
