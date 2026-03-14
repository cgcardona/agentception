from __future__ import annotations

"""Worktree reaper — removes orphaned agent worktree directories.

``teardown_agent_worktree`` in ``services/teardown.py`` is called by agents
when they finish normally (via ``build_complete_run``).  Agents that crash or
are killed never reach that call, leaving their worktree directories on disk
indefinitely.

This module provides ``reap_stale_worktrees()``, which is called:
- Once at application startup (catches orphans from the previous session).
- Every 15 minutes by a background asyncio task (catches orphans from the
  current session).

**Important:** the reaper calls ``release_worktree`` (remove directory + prune
refs only), NOT ``teardown_agent_worktree`` (which also deletes remote and
local git branches).  Deleting the remote branch of a run that still has an
open PR would cause GitHub to auto-close that PR — a side effect the reaper
must never trigger.  Branch deletion is the responsibility of the merge/close
workflow, not the disk-space cleanup pass.
"""

import logging
from pathlib import Path

from agentception.config import settings
from agentception.db.persist import clear_run_worktree_path
from agentception.db.queries import get_terminal_runs_with_worktrees
from agentception.services.teardown import release_worktree

logger = logging.getLogger(__name__)


async def reap_stale_worktrees() -> int:
    """Remove worktree directories for terminal runs that left them on disk.

    Queries the DB for runs with a terminal status (completed, failed,
    cancelled, stopped) that still have a ``worktree_path`` set, then releases
    any whose directories are present on disk.  Uses ``release_worktree``
    (directory removal + ref pruning only) — **never** deletes git branches,
    so open PRs are not closed as a side effect.

    Returns:
        The number of worktree directories released in this pass.
    """
    runs = await get_terminal_runs_with_worktrees()
    if not runs:
        logger.debug("ℹ️  worktree reaper: no terminal runs with live worktrees")
        return 0

    repo_dir = str(settings.repo_dir)
    reaped = 0
    for run in runs:
        worktree_path = run["worktree_path"]
        if not Path(worktree_path).exists():
            # Directory is already gone — clear the stale DB reference so this
            # run is never returned by get_terminal_runs_with_worktrees again.
            # Without this, the reaper logs a "stale" entry on every pass for
            # runs whose directories were cleaned up outside of release_worktree
            # (e.g. container restart, manual deletion).
            logger.info(
                "ℹ️  worktree reaper: dir absent, clearing stale DB ref for run %r",
                run["id"],
            )
            await clear_run_worktree_path(run["id"])
            continue
        logger.info(
            "⚠️  worktree reaper: releasing stale worktree dir for run %r at %s",
            run["id"],
            worktree_path,
        )
        if await release_worktree(worktree_path=worktree_path, repo_dir=repo_dir):
            await clear_run_worktree_path(run["id"])
            reaped += 1

    if reaped:
        logger.info("✅ worktree reaper: released %d stale worktree dir(s)", reaped)
    return reaped
