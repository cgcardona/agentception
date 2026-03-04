from __future__ import annotations

"""Async health collector with a 5-second result cache.

Gathers system metrics once per cache window and returns a HealthSnapshot.
The 5-second TTL prevents concurrent dashboard polls from hammering the OS
and GitHub API simultaneously.

Cache state is module-level so it persists across requests within a process.
The asyncio.Lock guarantees that concurrent callers wait for the in-progress
gather rather than spawning duplicate probes.
"""

import asyncio
import logging
import subprocess
import time

import psutil

from agentception.models.health import HealthSnapshot

logger = logging.getLogger(__name__)

_PROCESS_START: float = time.monotonic()
_CACHE_TTL_SECONDS: float = 5.0

_lock: asyncio.Lock = asyncio.Lock()
_cached: HealthSnapshot | None = None
_cached_at: float = 0.0


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


async def _gather() -> HealthSnapshot:
    """Collect all health metrics. Must be called under the cache lock."""
    uptime = time.monotonic() - _PROCESS_START
    memory_rss_mb = _memory_rss_mb()
    worktree_count = _active_worktree_count()
    latency_ms = await _github_api_latency_ms()

    logger.debug(
        "Health collected: uptime=%.1fs mem=%.1fMB worktrees=%d gh_latency=%.1fms",
        uptime,
        memory_rss_mb,
        worktree_count,
        latency_ms,
    )
    return HealthSnapshot(
        uptime_seconds=uptime,
        memory_rss_mb=memory_rss_mb,
        active_worktree_count=worktree_count,
        github_api_latency_ms=latency_ms,
    )


def _memory_rss_mb() -> float:
    """Return the RSS of the current process in megabytes."""
    try:
        return psutil.Process().memory_info().rss / (1024.0 * 1024.0)
    except psutil.Error as exc:
        logger.warning("⚠️ Failed to read memory RSS: %s", exc)
        return 0.0


def _active_worktree_count() -> int:
    """Count active git worktrees via 'git worktree list --porcelain'.

    Each worktree block in the porcelain output begins with a line starting
    with 'worktree ', so counting those lines gives the total.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning("⚠️ git worktree list exited %d", result.returncode)
            return 0
        return result.stdout.count("worktree ")
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("⚠️ Failed to count worktrees: %s", exc)
        return 0


async def _github_api_latency_ms() -> float:
    """Probe the GitHub API and return round-trip latency in milliseconds.

    Uses the unauthenticated /rate_limit endpoint — lightweight, always
    available, and requires no credentials.  Returns -1.0 when the probe
    fails or when httpx is not installed.
    """
    try:
        import httpx  # optional — not required for core functionality

        start = time.monotonic()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.github.com/rate_limit")
            resp.raise_for_status()
        return round((time.monotonic() - start) * 1000.0, 2)
    except ImportError:
        logger.debug("httpx not available — skipping GitHub API latency probe")
        return -1.0
    except Exception as exc:
        logger.warning("⚠️ GitHub API probe failed: %s", exc)
        return -1.0
