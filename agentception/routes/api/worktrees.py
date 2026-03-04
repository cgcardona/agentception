"""API route: worktree deletion."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentception.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class DeleteWorktreeResult(BaseModel):
    """Result of removing a single worktree."""

    slug: str
    deleted: bool
    pruned: bool
    error: str | None = None


@router.delete("/worktrees/{slug}", tags=["control"])
async def delete_worktree(slug: str) -> DeleteWorktreeResult:
    """Remove a single linked worktree by its slug (directory name).

    Runs ``git worktree remove --force <path>`` followed by
    ``git worktree prune`` to keep git's internal reference list clean.
    The worktree's branch is intentionally left intact so history is
    preserved; use the sweep endpoint to bulk-remove stale branches.
    """
    from agentception.readers.git import list_git_worktrees

    repo_dir = str(settings.repo_dir)
    worktrees = await list_git_worktrees()
    wt = next((w for w in worktrees if str(w.get("slug", "")) == slug), None)
    if wt is None:
        raise HTTPException(status_code=404, detail=f"Worktree '{slug}' not found")
    if wt.get("is_main"):
        raise HTTPException(status_code=400, detail="Cannot delete the main worktree")

    wt_path = str(wt["path"])
    deleted = False
    pruned = False
    error: str | None = None

    # Unlock first — locked worktrees silently resist `remove --force`.
    if wt.get("locked"):
        unlock_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "worktree", "unlock", wt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await unlock_proc.communicate()

    # Remove the worktree directory (--force handles dirty working trees).
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "remove", "--force", wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        deleted = True
        logger.info("✅ Removed worktree %s", wt_path)
    else:
        error = stderr.decode().strip()
        logger.warning("⚠️  Failed to remove worktree %s: %s", wt_path, error)

    # Always attempt a prune pass to clean up git's internal metadata.
    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()
    pruned = prune_proc.returncode == 0

    return DeleteWorktreeResult(slug=slug, deleted=deleted, pruned=pruned, error=error)
