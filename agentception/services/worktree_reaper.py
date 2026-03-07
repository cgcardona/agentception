from __future__ import annotations

"""Worktree reaper — removes orphaned agent worktrees without agent cooperation.

``teardown_agent_worktree`` in ``services/teardown.py`` is called by agents
when they finish normally (via ``build_report_done``).  Agents that crash or
are killed never reach that call, leaving their worktrees on disk indefinitely.

This module provides ``reap_stale_worktrees()``, which is called:
- Once at application startup (catches orphans from the previous session).
- Every 15 minutes by a background asyncio task (catches orphans from the
  current session).

The reaper delegates all actual cleanup to the existing
``teardown_agent_worktree()`` function — it adds no new git logic.
"""

import logging
from pathlib import Path

from agentception.db.queries import get_terminal_runs_with_worktrees
from agentception.services.teardown import teardown_agent_worktree

logger = logging.getLogger(__name__)


async def reap_stale_worktrees() -> int:
    """Remove worktrees for all terminal runs whose directories still exist.

    Queries the DB for runs with status ``done`` or ``stale`` that still have
    a ``worktree_path`` set, then tears down any whose directories are present
    on disk.  Each teardown delegates to ``teardown_agent_worktree``, which
    handles git worktree removal, branch deletion, and ref pruning — and
    swallows all errors so a single bad worktree never blocks the sweep.

    Returns:
        The number of worktrees reaped in this pass.
    """
    runs = await get_terminal_runs_with_worktrees()
    if not runs:
        logger.debug("ℹ️  worktree reaper: no terminal runs with live worktrees")
        return 0

    reaped = 0
    for run in runs:
        worktree_path = run["worktree_path"]
        if not Path(worktree_path).exists():
            continue
        logger.info(
            "⚠️  worktree reaper: stale worktree found for run %r at %s — tearing down",
            run["id"],
            worktree_path,
        )
        await teardown_agent_worktree(run["id"])
        reaped += 1

    if reaped:
        logger.info("✅ worktree reaper: reaped %d stale worktree(s)", reaped)
    return reaped
