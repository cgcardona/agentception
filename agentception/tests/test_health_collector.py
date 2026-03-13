"""Unit tests for agentception.services.health_collector.

Covers the full service layer that backs the /api/health/detailed endpoint:

  _memory_rss_mb()          — returns a non-negative float from the OS
  _active_worktree_count()  — counts subdirectories; ignores files; 0 when absent
  _probe_github_latency_ms() — returns -1.0 on any network/timeout error
  collect()                  — returns HealthSnapshot; respects the 5-second cache

All I/O is mocked so these tests run offline and without touching the filesystem
beyond the tmp_path fixtures.

Run targeted:
    pytest agentception/tests/test_health_collector.py -v
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import agentception.services.health_collector as hc
from agentception.models.health import HealthSnapshot

# ── Helpers ───────────────────────────────────────────────────────────────────

_SNAPSHOT = HealthSnapshot(
    uptime_seconds=10.0,
    memory_rss_mb=64.0,
    active_worktree_count=2,
    github_api_latency_ms=42.0,
)


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset the module-level cache globals before every test.

    Without this, a successful ``collect()`` in one test contaminates the next
    by leaving a warm cache entry that prevents ``_gather`` from being called.
    """
    hc._cached = None
    hc._cached_at = 0.0


# ── _memory_rss_mb ────────────────────────────────────────────────────────────


def test_memory_rss_mb_returns_nonneg_float() -> None:
    """_memory_rss_mb() must return a float >= 0.0 using the real OS resource call."""
    result = hc._memory_rss_mb()
    assert isinstance(result, float)
    assert result >= 0.0


# ── _active_worktree_count ────────────────────────────────────────────────────


def test_active_worktree_count_returns_zero_when_dir_absent(tmp_path: Path) -> None:
    """_active_worktree_count() must return 0 when the worktrees directory does not exist."""
    missing = tmp_path / "nonexistent-worktrees"
    with patch("agentception.services.health_collector.settings") as mock_settings:
        mock_settings.worktrees_dir = missing
        assert hc._active_worktree_count() == 0


def test_active_worktree_count_counts_subdirectories(tmp_path: Path) -> None:
    """_active_worktree_count() must return the exact number of subdirectories."""
    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()
    (wt_dir / "issue-1").mkdir()
    (wt_dir / "issue-2").mkdir()
    (wt_dir / "issue-3").mkdir()
    with patch("agentception.services.health_collector.settings") as mock_settings:
        mock_settings.worktrees_dir = wt_dir
        assert hc._active_worktree_count() == 3


def test_active_worktree_count_ignores_plain_files(tmp_path: Path) -> None:
    """_active_worktree_count() must count only directories, not regular files."""
    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()
    (wt_dir / "issue-1").mkdir()
    (wt_dir / "README.md").write_text("not a worktree", encoding="utf-8")
    with patch("agentception.services.health_collector.settings") as mock_settings:
        mock_settings.worktrees_dir = wt_dir
        assert hc._active_worktree_count() == 1


# ── _probe_github_latency_ms ──────────────────────────────────────────────────


def _mock_httpx_client(side_effect: Exception | None = None) -> MagicMock:
    """Build a mock httpx.AsyncClient context-manager for async with usage."""
    instance = AsyncMock()
    if side_effect is not None:
        instance.head.side_effect = side_effect
    else:
        instance.head.return_value = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=instance)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.anyio
async def test_probe_github_latency_ms_returns_minus_one_on_connect_error() -> None:
    """-1.0 is returned when the GitHub probe raises a connection error."""
    with patch(
        "agentception.services.health_collector.httpx.AsyncClient",
        return_value=_mock_httpx_client(httpx.ConnectError("refused")),
    ):
        result = await hc._probe_github_latency_ms()
    assert result == -1.0


@pytest.mark.anyio
async def test_probe_github_latency_ms_returns_minus_one_on_timeout() -> None:
    """-1.0 is returned when the GitHub probe times out."""
    with patch(
        "agentception.services.health_collector.httpx.AsyncClient",
        return_value=_mock_httpx_client(httpx.ReadTimeout("timed out")),
    ):
        result = await hc._probe_github_latency_ms()
    assert result == -1.0


@pytest.mark.anyio
async def test_probe_github_latency_ms_returns_positive_float_on_success() -> None:
    """A successful probe must return a non-negative float (measured latency in ms)."""
    with patch(
        "agentception.services.health_collector.httpx.AsyncClient",
        return_value=_mock_httpx_client(),
    ):
        result = await hc._probe_github_latency_ms()
    assert isinstance(result, float)
    assert result >= 0.0


# ── collect ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_collect_returns_health_snapshot_instance() -> None:
    """collect() must return a HealthSnapshot regardless of the gathered values."""
    with patch.object(hc, "_gather", new_callable=AsyncMock, return_value=_SNAPSHOT):
        result = await hc.collect()
    assert isinstance(result, HealthSnapshot)


@pytest.mark.anyio
async def test_collect_hits_cache_within_ttl() -> None:
    """collect() must not call _gather a second time when the cache is still fresh.

    The cache is considered fresh when the elapsed time since the last gather is
    less than _CACHE_TTL_SECONDS (5 s).  Two rapid back-to-back calls must
    produce exactly one _gather invocation.
    """
    hc._cached = _SNAPSHOT
    hc._cached_at = time.monotonic()  # just warmed — well within TTL

    with patch.object(hc, "_gather", new_callable=AsyncMock, return_value=_SNAPSHOT) as mock_gather:
        r1 = await hc.collect()
        r2 = await hc.collect()

    assert r1 is _SNAPSHOT
    assert r2 is _SNAPSHOT
    mock_gather.assert_not_awaited()


@pytest.mark.anyio
async def test_collect_refreshes_after_ttl_expires() -> None:
    """collect() must call _gather again once the cache has aged past the TTL."""
    stale = HealthSnapshot(
        uptime_seconds=1.0,
        memory_rss_mb=10.0,
        active_worktree_count=0,
        github_api_latency_ms=5.0,
    )
    fresh = HealthSnapshot(
        uptime_seconds=100.0,
        memory_rss_mb=20.0,
        active_worktree_count=1,
        github_api_latency_ms=12.0,
    )
    hc._cached = stale
    hc._cached_at = time.monotonic() - (hc._CACHE_TTL_SECONDS + 1.0)  # expired

    with patch.object(hc, "_gather", new_callable=AsyncMock, return_value=fresh) as mock_gather:
        result = await hc.collect()

    assert result is fresh
    mock_gather.assert_awaited_once()
