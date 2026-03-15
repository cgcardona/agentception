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

**Branch deletion policy:**

- Issue-scoped runs (``issue-*`` run IDs) may have an open PR on their branch.
  The reaper uses ``release_worktree`` (directory + prune only) and never
  deletes the branch, so open PRs are not accidentally closed.

- Label/coordinator runs (``label-*`` run IDs) dispatch against the full
  initiative label and use ``agent/<slug>`` branches that never back a PR.
  When the reaper finds a stale worktree for a label run it is safe to also
  delete the remote and local branch to prevent accumulation on GitHub.
"""

import asyncio
import logging
from pathlib import Path

from agentception.config import settings
from agentception.db.persist import clear_run_worktree_path
from agentception.db.queries import get_terminal_runs_with_worktrees
from agentception.services.teardown import _prune_worktree_refs, release_worktree

logger = logging.getLogger(__name__)

_LABEL_RUN_PREFIX = "label-"


async def _delete_label_branch(branch: str, repo_dir: str) -> None:
    """Delete the remote and local branch for a stale label/coordinator run.

    Label agents use ``agent/<slug>`` branches that never back a PR, so it is
    safe to delete them when the worktree is reaped.  Failures are non-fatal
    and logged as info (branch may already be gone).
    """
    push_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "push", "origin", "--delete", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, push_err = await push_proc.communicate()
    if push_proc.returncode == 0:
        logger.info("✅ reaper: deleted remote branch %r for stale label run", branch)
    else:
        logger.info(
            "ℹ️  reaper: remote branch %r already gone or not pushed: %s",
            branch,
            push_err.decode().strip(),
        )

    branch_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "branch", "-D", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, branch_err = await branch_proc.communicate()
    if branch_proc.returncode == 0:
        logger.info("✅ reaper: deleted local branch %r for stale label run", branch)
    else:
        logger.info(
            "ℹ️  reaper: local branch %r already gone: %s",
            branch,
            branch_err.decode().strip(),
        )


async def reap_stale_worktrees() -> int:
    """Remove worktree directories for terminal runs that left them on disk.

    Queries the DB for runs with a terminal status (completed, failed,
    cancelled, stopped) that still have a ``worktree_path`` set, then releases
    any whose directories are present on disk.

    For issue-scoped runs, uses ``release_worktree`` (directory removal + ref
    pruning only) — branches are preserved so open PRs are not closed.

    For label/coordinator runs (``label-*`` run IDs), also deletes the remote
    and local ``agent/<slug>`` branch, which never backs a PR and would
    otherwise accumulate on GitHub indefinitely.

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
        run_id = run["id"]
        worktree_path = run["worktree_path"]
        branch = run["branch"]
        is_label_run = run_id.startswith(_LABEL_RUN_PREFIX)

        if not Path(worktree_path).exists():
            # Directory is already gone — clear the stale DB reference so this
            # run is never returned by get_terminal_runs_with_worktrees again.
            logger.info(
                "ℹ️  worktree reaper: dir absent, clearing stale DB ref for run %r",
                run_id,
            )
            await clear_run_worktree_path(run_id)
            if is_label_run and branch:
                await _delete_label_branch(branch, repo_dir)
            # Prune even when the dir was already gone — stale git metadata may remain.
            await _prune_worktree_refs(repo_dir)
            continue

        logger.info(
            "⚠️  worktree reaper: releasing stale worktree for run %r at %s (label_run=%s)",
            run_id,
            worktree_path,
            is_label_run,
        )
        if await release_worktree(worktree_path=worktree_path, repo_dir=repo_dir):
            await clear_run_worktree_path(run_id)
            if is_label_run and branch:
                await _delete_label_branch(branch, repo_dir)
            reaped += 1

    if reaped:
        logger.info("✅ worktree reaper: released %d stale worktree dir(s)", reaped)
    return reaped
