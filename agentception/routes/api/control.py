"""API routes: control plane — pause/resume, label pins, sweep, reset-build, trigger-poll."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from agentception.config import settings
from agentception.readers.active_label_override import clear_pin, get_pin, set_pin
from agentception.readers.github import get_active_label
from agentception.readers.pipeline_config import read_pipeline_config

logger = logging.getLogger(__name__)

# Sentinel file that pauses agent spawning when present.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"

router = APIRouter()


class ActiveLabelRequest(BaseModel):
    label: str


class ActiveLabelStatus(BaseModel):
    label: str | None
    pinned: bool
    pin: str | None


@router.post("/control/pause", tags=["control"])
async def pause_pipeline() -> JSONResponse:
    """Create the pipeline-pause sentinel file, halting agent spawning.

    Idempotent — calling pause when already paused is a no-op.
    """
    _SENTINEL.touch()
    hx_trigger = json.dumps({"toast": {"message": "Pipeline paused", "type": "warning"}})
    return JSONResponse(content={"paused": True}, headers={"HX-Trigger": hx_trigger})


@router.post("/control/resume", tags=["control"])
async def resume_pipeline() -> JSONResponse:
    """Remove the pipeline-pause sentinel file, allowing agent spawning to continue.

    Idempotent — calling resume when not paused is a no-op.
    """
    _SENTINEL.unlink(missing_ok=True)
    hx_trigger = json.dumps({"toast": {"message": "Pipeline resumed", "type": "success"}})
    return JSONResponse(content={"paused": False}, headers={"HX-Trigger": hx_trigger})


@router.get("/control/status", tags=["control"])
async def control_status() -> dict[str, bool]:
    """Return the current pause state of the agent pipeline."""
    return {"paused": _SENTINEL.exists()}


@router.get("/control/active-label", tags=["control"])
async def get_active_label_status() -> ActiveLabelStatus:
    """Return the current active label and whether it is manually pinned."""
    pin = get_pin()
    resolved = await get_active_label()
    return ActiveLabelStatus(label=resolved, pinned=pin is not None, pin=pin)


@router.put("/control/active-label", tags=["control"])
async def pin_active_label(body: ActiveLabelRequest) -> ActiveLabelStatus:
    """Manually pin the active phase label, overriding automatic selection."""
    config = await read_pipeline_config()
    if body.label not in config.active_labels_order:
        raise HTTPException(
            status_code=400,
            detail=f"Label '{body.label}' not in active_labels_order. "
                   f"Valid: {config.active_labels_order}",
        )
    set_pin(body.label)
    logger.info("📌 Active label pinned to '%s'", body.label)
    return ActiveLabelStatus(label=body.label, pinned=True, pin=body.label)


@router.delete("/control/active-label", tags=["control"])
async def unpin_active_label() -> ActiveLabelStatus:
    """Clear the manual pin and return to automatic phase selection."""
    clear_pin()
    resolved = await get_active_label()
    logger.info("🔓 Active label pin cleared, auto-resolved to '%s'", resolved)
    return ActiveLabelStatus(label=resolved, pinned=False, pin=None)


class ResetBuildResult(BaseModel):
    """Result of a full build reset (worktrees, wip labels, run status)."""

    removed_worktrees: list[str]
    cleared_wip_labels: list[int]
    runs_reset: int
    errors: list[str]


@router.post("/control/reset-build", tags=["control"])
async def reset_build() -> ResetBuildResult:
    """Remove all agent worktrees, clear all agent/wip labels, and reset run statuses.

    Use this to start over from scratch: no worktrees, no in-motion labels,
    and no pending_launch/implementing/reviewing runs in the DB. The main
    worktree is never removed. Idempotent when already clean.
    """
    from agentception.db.persist import reset_build_runs_to_failed
    from agentception.readers.git import list_git_worktrees
    from agentception.readers.github import clear_wip_label, get_wip_issues

    removed_worktrees: list[str] = []
    cleared_wip_labels: list[int] = []
    errors: list[str] = []
    worktrees_dir_str = str(settings.worktrees_dir).rstrip("/")
    repo_dir = str(settings.repo_dir)

    # ── 1. Remove every non-main worktree under worktrees_dir ───────────────
    all_wts = await list_git_worktrees()
    for wt in all_wts:
        if wt.get("is_main"):
            continue
        path = str(wt.get("path", "")).rstrip("/")
        if not path or (path != worktrees_dir_str and not path.startswith(worktrees_dir_str + "/")):
            continue
        slug = str(wt.get("slug", ""))
        if wt.get("locked"):
            unlock_proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_dir, "worktree", "unlock", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await unlock_proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "worktree", "remove", "--force", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            removed_worktrees.append(slug or path)
            logger.info("✅ reset-build: removed worktree %s", path)
        else:
            errors.append(f"worktree {slug}: {stderr.decode().strip()}")

    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()

    # ── 2. Clear agent/wip from every issue that has it ────────────────────
    try:
        wip_issues = await get_wip_issues()
        for issue in wip_issues:
            num = issue.get("number")
            if not isinstance(num, int):
                continue
            try:
                await clear_wip_label(num)
                cleared_wip_labels.append(num)
            except Exception as exc:
                errors.append(f"clear wip #{num}: {exc}")
    except Exception as exc:
        errors.append(f"get_wip_issues: {exc}")

    # ── 3. Set all active runs to failed ───────────────────────────────────
    runs_reset = await reset_build_runs_to_failed()

    logger.info(
        "✅ reset-build complete: worktrees=%d wip_cleared=%d runs_reset=%d errors=%d",
        len(removed_worktrees),
        len(cleared_wip_labels),
        runs_reset,
        len(errors),
    )
    return ResetBuildResult(
        removed_worktrees=removed_worktrees,
        cleared_wip_labels=cleared_wip_labels,
        runs_reset=runs_reset,
        errors=errors,
    )


class SweepResult(BaseModel):
    """Result of a stale-state sweep operation."""

    deleted_branches: list[str]
    removed_worktrees: list[str]
    cleared_wip_labels: list[int]
    errors: list[str]


@router.post("/control/sweep", tags=["control"])
async def sweep_stale(dry_run: bool = False) -> SweepResult:
    """Delete all stale agent branches, remove orphan worktrees, and clear stale agent/wip labels.

    A branch is stale when it is an agent branch with no live git worktree.
    A claim is stale when an issue carries ``agent/wip`` but has no matching worktree.

    Parameters
    ----------
    dry_run:
        When ``True``, return what *would* be deleted without making any changes.
    """
    from agentception.readers.git import list_git_branches, list_git_worktrees
    from agentception.readers.github import clear_wip_label, get_wip_issues
    from agentception.intelligence.guards import detect_stale_claims

    deleted_branches: list[str] = []
    removed_worktrees: list[str] = []
    cleared_wip_labels: list[int] = []
    errors: list[str] = []

    repo_dir = str(settings.repo_dir)

    # ── 1. Stale branches (agent branch with no live worktree) ───────────────
    live_branches: set[str] = {
        str(wt.get("branch", ""))
        for wt in await list_git_worktrees()
        if wt.get("branch") and not wt.get("is_main")
    }

    for branch in await list_git_branches():
        name = str(branch.get("name", "")).strip()
        if not branch.get("is_agent_branch"):
            continue
        if name in live_branches:
            continue  # has a live worktree — not stale
        deleted_branches.append(name)
        if not dry_run:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_dir, "branch", "-D", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                errors.append(f"branch -D {name}: {stderr.decode().strip()}")
                deleted_branches.pop()

    # ── 2. Prune git's internal worktree references ───────────────────────
    if not dry_run:
        prune_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "worktree", "prune",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        prune_out, prune_err = await prune_proc.communicate()
        pruned = (prune_out + prune_err).decode().strip()
        if pruned:
            removed_worktrees.append(f"pruned: {pruned}")

    # ── 3. Stale agent/wip labels ────────────────────────────────────────
    try:
        wip_issues = await get_wip_issues()
        stale_claims = await detect_stale_claims(wip_issues, settings.worktrees_dir)
        for claim in stale_claims:
            cleared_wip_labels.append(claim.issue_number)
            if not dry_run:
                try:
                    await clear_wip_label(claim.issue_number)
                except Exception as exc:
                    errors.append(f"clear wip #{claim.issue_number}: {exc}")
                    cleared_wip_labels.pop()
    except Exception as exc:
        errors.append(f"stale claims check: {exc}")

    action = "Would delete" if dry_run else "Swept"
    logger.info(
        "✅ %s: branches=%s wip_labels=%s errors=%d",
        action, deleted_branches, cleared_wip_labels, len(errors),
    )
    return SweepResult(
        deleted_branches=deleted_branches,
        removed_worktrees=removed_worktrees,
        cleared_wip_labels=cleared_wip_labels,
        errors=errors,
    )


@router.post("/control/trigger-poll", tags=["control"])
async def trigger_poll() -> JSONResponse:
    """Fire an immediate poller tick, refreshing pipeline state.

    The tick runs asynchronously; the response returns immediately.
    """
    from agentception.poller import tick as _tick

    asyncio.create_task(_tick())
    logger.info("✅ Manual poll tick triggered via /control/trigger-poll")
    hx_trigger = json.dumps({"toast": {"message": "Poll triggered", "type": "info"}})
    return JSONResponse(content={"triggered": True}, headers={"HX-Trigger": hx_trigger})
