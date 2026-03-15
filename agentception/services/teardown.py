from __future__ import annotations

"""Agent worktree teardown service.

Called from two places:
- ``agentception/routes/api/runs.py`` — HTTP ``POST /api/runs/{run_id}/done``
- ``agentception/mcp/build_tools.py`` — MCP ``build_report_done``

Keeping teardown here prevents the MCP layer from importing the routes layer
(a layering violation) and keeps the git operations in one place.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from agentception.config import settings
from agentception.db.queries import get_agent_run_teardown
from agentception.services.run_factory import _WORKTREE_COLLECTION_PREFIX

logger = logging.getLogger(__name__)


async def release_worktree(worktree_path: str, repo_dir: str) -> bool:
    """Remove the worktree directory and prune stale refs — branches untouched.

    Used by :func:`build_complete_run` immediately before dispatching the PR
    reviewer.  The reviewer needs to check out the same branch in its own
    worktree; git forbids two worktrees sharing a branch, so the executor's
    worktree must be released first.  Branches are intentionally **not** deleted
    here because the open PR still references the remote branch.

    Safe to call even if the worktree dir no longer exists (idempotent).

    Returns True if the worktree was removed or was already gone; False if
    ``git worktree remove`` failed (caller may retry or clear DB anyway).
    """
    repo = repo_dir
    if Path(worktree_path).exists():
        rm_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo, "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await rm_proc.communicate()
        if rm_proc.returncode == 0:
            logger.info("✅ release_worktree: removed %s", worktree_path)
        else:
            # git rejects directories that are no longer registered in its
            # worktree list (e.g. after a container restart that reset the git
            # process state).  Fall back to shutil.rmtree — the directory is
            # orphaned and safe to force-remove.
            logger.warning(
                "⚠️  release_worktree: git worktree remove failed (%s) — "
                "falling back to shutil.rmtree for %s",
                stderr.decode().strip(),
                worktree_path,
            )
            try:
                shutil.rmtree(worktree_path)
                logger.info("✅ release_worktree: force-removed %s via shutil.rmtree", worktree_path)
            except Exception as rm_exc:
                logger.warning(
                    "⚠️  release_worktree: shutil.rmtree also failed for %s: %s",
                    worktree_path,
                    rm_exc,
                )
                prune_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", repo, "worktree", "prune",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await prune_proc.communicate()
                return False
    else:
        logger.info("ℹ️  release_worktree: %s already gone — skipping", worktree_path)

    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()
    return True  # removed or already gone


async def teardown_agent_worktree(run_id: str) -> None:
    """Remove the worktree, prune refs, and delete the branch for a finished agent.

    Safe to call from any context — all errors are logged and swallowed so a
    cleanup failure never breaks the caller.  Idempotent: if the worktree or
    branch is already gone, each step logs an info message and continues.

    Steps:
    1. ``git worktree remove --force <worktree_path>``
    2. ``git worktree prune``
    3. ``git push origin --delete <branch>`` (remote branch)
    4. ``git branch -D <branch>`` (local branch ref in the main repo)
    """
    teardown = await get_agent_run_teardown(run_id)
    if teardown is None:
        logger.warning("⚠️  teardown_agent_worktree: no DB row for run_id=%r", run_id)
        return

    repo_dir = str(settings.repo_dir)
    worktree_path = teardown["worktree_path"]
    branch = teardown["branch"]

    if worktree_path and Path(worktree_path).exists():
        rm_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await rm_proc.communicate()
        if rm_proc.returncode == 0:
            logger.info("✅ teardown[%s]: removed worktree %s", run_id, worktree_path)
        else:
            logger.warning(
                "⚠️  teardown[%s]: worktree remove failed: %s",
                run_id,
                stderr.decode().strip(),
            )

    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()

    if branch:
        push_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "push", "origin", "--delete", branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, push_stderr = await push_proc.communicate()
        if push_proc.returncode == 0:
            logger.info("✅ teardown[%s]: deleted remote branch %r", run_id, branch)
        else:
            logger.info(
                "ℹ️  teardown[%s]: remote branch %r already gone or not pushed: %s",
                run_id,
                branch,
                push_stderr.decode().strip(),
            )

        branch_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "branch", "-D", branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, branch_stderr = await branch_proc.communicate()
        if branch_proc.returncode == 0:
            logger.info("✅ teardown[%s]: deleted local branch %r", run_id, branch)
        else:
            logger.info(
                "ℹ️  teardown[%s]: local branch %r already gone: %s",
                run_id,
                branch,
                branch_stderr.decode().strip(),
            )

    # Best-effort: delete the per-run Qdrant worktree collection to reclaim space.
    await _prune_worktree_collection(run_id)

    logger.info("✅ teardown[%s]: complete", run_id)


async def _prune_worktree_collection(run_id: str) -> None:
    """Delete the ``worktree-<run_id>`` Qdrant collection created at spawn time.

    Non-blocking and error-swallowing — a failed prune leaves a stale
    collection that can be cleaned up manually; it never breaks teardown.
    """
    collection = f"{_WORKTREE_COLLECTION_PREFIX}{run_id}"
    try:
        from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

        client = AsyncQdrantClient(url=settings.qdrant_url)
        collections = await client.get_collections()
        existing = {c.name for c in collections.collections}
        if collection not in existing:
            logger.info(
                "ℹ️  teardown[%s]: worktree collection %r not found — nothing to prune",
                run_id,
                collection,
            )
            return
        await client.delete_collection(collection)
        logger.info("✅ teardown[%s]: pruned worktree collection %r", run_id, collection)
    except Exception as exc:
        logger.warning(
            "⚠️  teardown[%s]: could not prune worktree collection %r: %s",
            run_id,
            collection,
            exc,
        )
