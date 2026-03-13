"""health_collector — gathers system health metrics with a 5-second result cache.

Single public coroutine: ``collect() -> HealthSnapshot``.

Cache semantics: results are reused for ``_CACHE_TTL_SECONDS`` (5 s).  An
``asyncio.Lock`` prevents concurrent callers from spawning duplicate gathers —
the second coroutine waits and then receives the freshly-computed value.

Metrics gathered:
- ``uptime_seconds``         — wall-clock seconds since this module was imported.
- ``memory_rss_mb``         — process RSS via ``resource.getrusage`` (stdlib, no psutil).
- ``active_worktree_count`` — subdirectory count under ``settings.worktrees_dir``.
- ``github_api_latency_ms`` — round-trip to ``https://api.github.com`` via httpx.
                               Returns -1.0 on any network or timeout error.
"""
from __future__ import annotations

import asyncio
import logging
import resource
import sys
import time

import httpx

from agentception.config import settings
from agentception.models.health import HealthSnapshot

logger = logging.getLogger(__name__)

# Module-load time used as the process start reference for uptime.
_START_TIME: float = time.monotonic()

# GitHub probe endpoint — just the root, cheap and always available.
_GITHUB_PROBE_URL: str = "https://api.github.com"
_PROBE_TIMEOUT_S: float = 5.0

# Cache
_CACHE_TTL_SECONDS: float = 5.0
_lock: asyncio.Lock = asyncio.Lock()
_cached: HealthSnapshot | None = None
_cached_at: float = 0.0


def _memory_rss_mb() -> float:
    """Return the process Resident Set Size in megabytes.

    ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` returns:
    - bytes on macOS (darwin)
    - kilobytes on Linux (including Docker containers)
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux (Docker) reports kilobytes.
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return rss / divisor


def _active_worktree_count() -> int:
    """Count direct subdirectories of ``settings.worktrees_dir``.

    Each checkout lives in its own subdirectory (e.g. ``issue-123/``).
    Returns 0 if the directory does not exist yet.
    """
    worktrees_dir = settings.worktrees_dir
    if not worktrees_dir.exists():
        return 0
    return sum(1 for p in worktrees_dir.iterdir() if p.is_dir())


async def _probe_github_latency_ms() -> float:
    """Return the round-trip time of a HEAD request to the GitHub API root.

    Returns -1.0 if the request fails or times out — the endpoint contract
    documents -1.0 as the sentinel for "probe has not yet run or failed."
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            start = time.monotonic()
            await client.head(_GITHUB_PROBE_URL)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return round(elapsed_ms, 3)
    except Exception:
        logger.warning("⚠️ GitHub API latency probe failed — returning -1.0")
        return -1.0


async def _gather() -> HealthSnapshot:
    """Collect all health metrics. Must be called under the cache lock."""
    uptime = round(time.monotonic() - _START_TIME, 3)
    memory = round(_memory_rss_mb(), 3)
    worktrees = _active_worktree_count()
    latency = await _probe_github_latency_ms()

    logger.debug(
        "✅ Health snapshot collected: uptime=%.1fs mem=%.1fMB worktrees=%d gh_latency=%.1fms",
        uptime,
        memory,
        worktrees,
        latency,
    )

    return HealthSnapshot(
        uptime_seconds=uptime,
        memory_rss_mb=memory,
        active_worktree_count=worktrees,
        github_api_latency_ms=latency,
    )


async def collect() -> HealthSnapshot:
    """Return a HealthSnapshot, using a cached result if fresher than 5 seconds.

    Thread-safe via asyncio.Lock: a second coroutine arriving while a gather
    is in progress will wait and then receive the freshly-computed value rather
    than firing its own redundant gather.
    """
    global _cached, _cached_at

    async with _lock:
        now = time.monotonic()
        if _cached is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached

        snapshot = await _gather()
        _cached = snapshot
        _cached_at = now
        return snapshot
