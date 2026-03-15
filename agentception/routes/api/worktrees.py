"""API route: worktree deletion."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentception.config import settings
from agentception.db.persist import clear_run_worktree_path
from agentception.db.queries import get_run_id_for_worktree_path

logger = logging.getLogger(__name__)

router = APIRouter()


class DeleteWorktreeResult(BaseModel):
    """Result of removing a single worktree."""

    slug: str
    deleted: bool
    pruned: bool
    db_cleared: bool = False
    error: str | None = None


@router.delete("/worktrees/{slug}", tags=["control"])
async def delete_worktree(slug: str) -> DeleteWorktreeResult:
    """Remove a single linked worktree by its slug (directory name).

    Runs ``git worktree unlock`` (if locked) then
    ``git worktree remove --force <path>`` with a ``shutil.rmtree`` fallback,
    followed by ``git worktree prune`` to keep git's internal reference list
    clean.  Also NULLs the DB ``worktree_path`` for the corresponding run so
    the reaper does not re-process it.

    The worktree's branch is intentionally left intact so history is
    preserved.
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
    db_cleared = False
    error: str | None = None

    # Unlock first — a single --force is insufficient for locked worktrees.
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
        git_err = stderr.decode().strip()
        logger.warning("⚠️  git worktree remove failed for %s: %s — trying shutil.rmtree", wt_path, git_err)
        if Path(wt_path).exists():
            try:
                shutil.rmtree(wt_path)
                deleted = True
                logger.info("✅ Force-removed worktree %s via shutil.rmtree", wt_path)
            except Exception as rm_exc:
                error = f"git: {git_err} | rmtree: {rm_exc}"
                logger.warning("⚠️  shutil.rmtree also failed for %s: %s", wt_path, rm_exc)
        else:
            # Directory already gone — consider it deleted.
            deleted = True

    # Always prune git's internal metadata regardless of how the dir was removed.
    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()
    pruned = prune_proc.returncode == 0

    # Clear the DB worktree_path so the reaper never re-processes this run.
    if deleted:
        run_id = await get_run_id_for_worktree_path(wt_path)
        if run_id:
            db_cleared = await clear_run_worktree_path(run_id)

    return DeleteWorktreeResult(slug=slug, deleted=deleted, pruned=pruned, db_cleared=db_cleared, error=error)
