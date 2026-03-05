"""API routes: control plane — pause/resume, label pins, spawn, wave, sweep, coordinator."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from agentception.config import settings
from agentception.models import (
    SpawnConductorRequest,
    SpawnConductorResult,
    SpawnCoordinatorRequest,
    SpawnCoordinatorResult,
    SpawnRequest,
    SpawnResult,
)
from agentception.readers.active_label_override import clear_pin, get_pin, set_pin
from agentception.readers.github import add_wip_label, get_active_label, get_issue, get_issue_body, get_open_issues
from agentception.readers.pipeline_config import read_pipeline_config
from ._shared import (
    _SENTINEL,
    _build_agent_task,
    _build_conductor_task,
    _build_coordinator_task,
    _issue_is_claimed_api,
    _resolve_cognitive_arch,
)

logger = logging.getLogger(__name__)

# Jinja2 template env for the HTML response path of POST /control/spawn.
# Kept local to this module so the API layer does not depend on the UI
# layer's _TEMPLATES singleton.  Both instances point at the same directory;
# Jinja2 environments are stateless w.r.t. file rendering.
_CTRL_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent.parent.parent / "templates")
)

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
    The CTO and Eng VP role files check for this sentinel at the top of
    every loop iteration and sleep instead of dispatching new agents.
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
    """Return the current pause state of the agent pipeline.

    Returns ``{"paused": true}`` when the sentinel file exists,
    ``{"paused": false}`` otherwise.
    """
    return {"paused": _SENTINEL.exists()}


@router.get("/control/active-label", tags=["control"])
async def get_active_label_status() -> ActiveLabelStatus:
    """Return the current active label and whether it is manually pinned.

    ``pinned`` is ``true`` when an operator override is in effect; ``false``
    means the label was determined automatically by scanning open issues.
    """
    pin = get_pin()
    resolved = await get_active_label()
    return ActiveLabelStatus(label=resolved, pinned=pin is not None, pin=pin)


@router.put("/control/active-label", tags=["control"])
async def pin_active_label(body: ActiveLabelRequest) -> ActiveLabelStatus:
    """Manually pin the active phase label, overriding automatic selection.

    The pin is held in memory for the lifetime of the AgentCeption process.
    Restart clears it and returns to auto mode.
    """
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


async def _do_spawn(body: SpawnRequest) -> SpawnResult:
    """Core spawn logic — validates, claims, creates worktree, and writes .agent-task.

    Extracted so both :func:`spawn_agent` (HTTP endpoint) and :func:`spawn_wave`
    (batch spawner) can call the same business logic without duplicating code or
    creating a circular dependency on the HTTP request/response layer.

    Raises
    ------
    HTTPException(404)
        When the issue does not exist on GitHub or is not open.
    HTTPException(409)
        When the issue is already claimed or the worktree directory exists.
    HTTPException(500)
        When ``git worktree add`` fails.
    """
    import re as _re

    issue_number = body.issue_number

    # ── 1. Fetch issue state from GitHub ──────────────────────────────────────
    try:
        issue = await get_issue(issue_number)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Issue #{issue_number} not found on GitHub: {exc}",
        ) from exc

    state = issue.get("state", "")
    if state != "OPEN":
        raise HTTPException(
            status_code=404,
            detail=f"Issue #{issue_number} is not open (state={state!r})",
        )

    # ── 2. Check whether the issue is already claimed ─────────────────────────
    raw_labels = issue.get("labels")
    # narrow from object to list before iterating — get() returns object
    label_names: list[str] = (
        [lbl for lbl in raw_labels if isinstance(lbl, str)]
        if isinstance(raw_labels, list)
        else []
    )
    if "agent:wip" in label_names:
        raise HTTPException(
            status_code=409,
            detail=f"Issue #{issue_number} is already claimed (agent:wip label present)",
        )

    # ── 3. Pre-flight: check for an existing worktree BEFORE claiming ─────────
    # Claiming (add_wip_label) is irreversible in the short term; checking the
    # worktree path first prevents leaving an issue permanently labelled
    # agent:wip with no agent actually working on it.
    branch = f"feat/issue-{issue_number}"
    worktree_path = settings.worktrees_dir / f"issue-{issue_number}"

    if worktree_path.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Worktree directory already exists: {worktree_path}. "
                "Remove it manually and retry."
            ),
        )

    # ── 4. Add agent:wip label ────────────────────────────────────────────────
    await add_wip_label(issue_number)

    repo_dir = str(settings.repo_dir)

    # Delete the local branch first if it already exists but has no live worktree.
    # This happens when a worktree was manually deleted without pruning the branch,
    # leaving `git worktree add -b` unable to create it again (exit 255).
    del_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "branch", "-D", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await del_proc.communicate()  # ignore errors — branch may not exist, that's fine

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "add", "-b", branch,
        str(worktree_path), "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(
                f"git worktree add failed (exit {proc.returncode}): "
                f"{stderr.decode().strip()!r}"
            ),
        )

    # Lock the worktree immediately so git doesn't auto-prune it when git
    # is run from a context where the container-internal path is not resolvable
    # (e.g. running `git worktree list` from the host sees /worktrees/issue-N
    # as missing since it's bind-mounted from ~/.agentception/worktrees/agentception/).
    lock_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "lock", str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await lock_proc.communicate()  # best-effort — non-fatal if it fails

    logger.info(
        "✅ Created worktree %s on branch %s for issue #%d",
        worktree_path,
        branch,
        issue_number,
    )

    # ── 5. Write .agent-task ──────────────────────────────────────────────────
    title_raw = issue.get("title", "")
    title: str = title_raw if isinstance(title_raw, str) else str(title_raw)

    # Fetch issue body for DEPENDS_ON extraction and COGNITIVE_ARCH derivation.
    try:
        issue_body = await get_issue_body(issue_number)
    except Exception:
        issue_body = ""

    # Extract "Depends on #NNN" patterns — comma-separated, or "none" if absent.
    dep_matches = _re.findall(r"[Dd]epends\s+on\s+#(\d+)", issue_body)
    depends_on = ",".join(dep_matches) if dep_matches else "none"

    # Derive COGNITIVE_ARCH from issue body so the agent gets the right persona.
    cognitive_arch = _resolve_cognitive_arch(issue_body, body.role)

    # Get active phase label for provenance — best-effort; not fatal if absent.
    try:
        phase_label = await get_active_label() or ""
    except Exception:
        phase_label = ""

    # Compute the host-side worktree path for display to the user.
    # host_worktrees_dir is ~/.agentception/worktrees/agentception (set via HOST_WORKTREES_DIR).
    host_worktree_path = settings.host_worktrees_dir / f"issue-{issue_number}"

    agent_task_content = _build_agent_task(
        issue_number=issue_number,
        title=title,
        role=body.role,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
        phase_label=phase_label,
        depends_on=depends_on,
        cognitive_arch=cognitive_arch,
    )
    task_file = worktree_path / ".agent-task"
    task_file.write_text(agent_task_content, encoding="utf-8")

    logger.info("✅ Wrote .agent-task to %s", task_file)

    spawned_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return SpawnResult(
        spawned=issue_number,
        worktree=str(worktree_path),
        host_worktree=str(host_worktree_path),
        branch=branch,
        agent_task=agent_task_content,
        spawned_at=spawned_at,
    )


@router.post("/control/spawn")
async def spawn_agent(request: Request, body: SpawnRequest) -> Response:
    """Manually seed a new engineer agent for an open, unclaimed issue.

    This endpoint bypasses the CTO/VP polling loop for direct human
    intervention — useful when an issue needs immediate attention or when
    the automated batch scheduler hasn't picked it up yet.

    The caller is responsible for launching the Cursor Task pointed at the
    returned ``worktree`` path.  This endpoint only prepares the worktree
    and ``.agent-task`` file; it does NOT start the agent automatically.

    When the ``Accept`` request header includes ``text/html`` the endpoint
    returns an HTML fragment (``_spawn_success.html``) suitable for an
    ``outerHTML`` swap.  All other callers receive the standard JSON
    ``SpawnResult`` body — the JSON path is unchanged.

    Raises
    ------
    HTTP 404
        When the issue number does not exist on GitHub or the issue is not
        open (already closed or merged).
    HTTP 409
        When the issue already carries an ``agent:wip`` label, indicating
        another agent has already claimed it.
    HTTP 500
        When the git worktree creation fails (e.g. directory already exists).
    """
    result = await _do_spawn(body)

    if "text/html" in request.headers.get("accept", ""):
        agent_id = f"issue-{result.spawned}"
        return _CTRL_TEMPLATES.TemplateResponse(
            request,
            "_spawn_success.html",
            {"result": result, "agent_id": agent_id},
        )

    return JSONResponse(content=result.model_dump())


class WaveSpawnResult(BaseModel):
    """Result of a batch spawn-wave operation."""

    active_label: str
    spawned: list[SpawnResult]
    skipped: list[dict[str, object]]  # issues skipped because already claimed / worktree exists


@router.post("/control/spawn-wave", tags=["control"])
async def spawn_wave(role: str = "python-developer") -> WaveSpawnResult:
    """Spawn agents for all unclaimed issues in the currently active phase.

    Reads the active phase label from ``pipeline-config.json``, fetches all
    open unclaimed issues carrying that label, and calls the single-spawn
    logic for each one.  Issues that are already claimed or already have a
    worktree are silently skipped (not an error).

    This is the "Start Wave" action available in the Overview dashboard.  The
    caller is still responsible for launching a Cursor Task pointed at each
    returned worktree path — this endpoint only creates the worktrees and
    ``.agent-task`` files.

    Parameters
    ----------
    role:
        Role file slug to assign to every spawned agent.  Defaults to
        ``python-developer``.  Must be a member of ``VALID_ROLES``.

    Raises
    ------
    HTTP 404
        When no active phase label is found in ``pipeline-config.json`` or
        no open issues carry that label.
    HTTP 422
        When ``role`` is not a recognised role slug.
    """
    from agentception.models import VALID_ROLES
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role {role!r}. Valid roles: {sorted(VALID_ROLES)}",
        )

    active_label = await get_active_label()
    if not active_label:
        raise HTTPException(
            status_code=404,
            detail=(
                "No active phase label found. Check that pipeline-config.json "
                "active_labels_order contains labels with open issues."
            ),
        )

    phase_issues = await get_open_issues(label=active_label)
    unclaimed = [iss for iss in phase_issues if not _issue_is_claimed_api(iss)]

    if not unclaimed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No unclaimed open issues found for label '{active_label}'. "
                "All issues may already be claimed or there are no open issues."
            ),
        )

    spawned: list[SpawnResult] = []
    skipped: list[dict[str, object]] = []

    for iss in unclaimed:
        issue_num = iss.get("number")
        if not isinstance(issue_num, int):
            continue
        try:
            result = await _do_spawn(SpawnRequest(issue_number=issue_num, role=role))
            spawned.append(result)
            logger.info("✅ Wave spawn: issue #%d → %s", issue_num, result.worktree)
        except HTTPException as exc:
            # 409 = already claimed or worktree exists → skip, not an error.
            if exc.status_code == 409:
                skipped.append({"issue_number": issue_num, "reason": exc.detail})
                logger.info("⏭️  Wave spawn skipped issue #%d: %s", issue_num, exc.detail)
            else:
                # Any other error (404 issue gone, 500 git failure) — skip and log.
                skipped.append({"issue_number": issue_num, "reason": exc.detail})
                logger.warning("⚠️  Wave spawn failed for issue #%d: %s", issue_num, exc.detail)

    logger.info(
        "✅ spawn-wave complete: label=%s spawned=%d skipped=%d",
        active_label, len(spawned), len(skipped),
    )
    return WaveSpawnResult(active_label=active_label, spawned=spawned, skipped=skipped)


class ResetBuildResult(BaseModel):
    """Result of a full build reset (worktrees, wip labels, run status)."""

    removed_worktrees: list[str]
    cleared_wip_labels: list[int]
    runs_reset: int
    errors: list[str]


@router.post("/control/reset-build", tags=["control"])
async def reset_build() -> ResetBuildResult:
    """Remove all agent worktrees, clear all agent:wip labels, and reset run statuses.

    Use this to start over from scratch: no worktrees, no in-motion labels,
    and no pending_launch/implementing/reviewing runs in the DB.  The main
    worktree is never removed.  Idempotent when already clean.
    """
    from agentception.db.persist import reset_build_runs_to_unknown
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

    # ── 2. Clear agent:wip from every issue that has it ────────────────────
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

    # ── 3. Set all active runs to unknown ───────────────────────────────────
    runs_reset = await reset_build_runs_to_unknown()

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
    """Delete all stale agent branches, remove orphan worktrees, and clear stale agent:wip labels.

    A branch is stale when it is an agent branch (``feat/issue-N`` or
        ``feat/plan-*``) with no live git worktree checked out on it.
    A claim is stale when an issue carries ``agent:wip`` but has no matching
    worktree directory.

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

    # ── 2. Prune git's internal worktree references (unlocked stale entries) ─
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

    # ── 3. Stale agent:wip labels (issue claimed but no worktree on disk) ────
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
    """Fire an immediate poller tick, refreshing pipeline state from the filesystem.

    Equivalent to what the overview page fires in the background on load.
    The tick runs asynchronously; the response returns immediately without
    waiting for the tick to complete.
    """
    from agentception.poller import tick as _tick

    asyncio.create_task(_tick())
    logger.info("✅ Manual poll tick triggered via /control/trigger-poll")
    hx_trigger = json.dumps({"toast": {"message": "Poll triggered", "type": "info"}})
    return JSONResponse(content={"triggered": True}, headers={"HX-Trigger": hx_trigger})


@router.post("/control/spawn-coordinator", tags=["control"])
async def spawn_coordinator(body: SpawnCoordinatorRequest) -> SpawnCoordinatorResult:
    """Seed a coordinator worktree from a free-form brain dump.

    Creates a git worktree and writes an ``.agent-task`` file that tells a
    Cursor background agent to run as coordinator using
    ``parallel-bugs-to-issues.md``.  The agent will:

    1. Run the Phase Planner against the ``BRAIN_DUMP`` field.
    2. Create required GitHub labels (phase-N/*, status/*, priority/*).
    3. Create one sub-worktree per phase-batch.
    4. Write ``.agent-task`` files for each sub-agent.
    5. Launch sub-agents via the Cursor Task tool.

    This endpoint only creates the worktree and task file.  The caller
    (AgentCeption UI) instructs the user to open the returned
    ``host_worktree`` path in Cursor to start the coordinator agent.

    Raises
    ------
    HTTP 409
        When a worktree with the same slug already exists.
    HTTP 422
        When ``brain_dump`` is empty.
    HTTP 500
        When ``git worktree add`` fails.
    """
    plan_text = body.plan_text.strip()
    if not plan_text:
        raise HTTPException(status_code=422, detail="plan_text must not be empty")

    now_slug = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = f"plan-{now_slug}"
    branch = f"feat/{slug}"
    worktree_path = settings.worktrees_dir / slug
    host_worktree_path = settings.host_worktrees_dir / slug

    if worktree_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Worktree '{slug}' already exists — wait a second and retry.",
        )

    repo_dir = str(settings.repo_dir)

    # Create the worktree on a fresh branch off origin/dev.
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "add", "-b", branch,
        str(worktree_path), "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(
                f"git worktree add failed (exit {proc.returncode}): "
                f"{stderr.decode().strip()!r}"
            ),
        )

    logger.info("✅ Created coordinator worktree %s on branch %s", worktree_path, branch)

    agent_task_content = _build_coordinator_task(
        slug=slug,
        plan_text=plan_text,
        label_prefix=body.label_prefix,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
    )
    task_file = worktree_path / ".agent-task"
    task_file.write_text(agent_task_content, encoding="utf-8")
    logger.info("✅ Wrote coordinator .agent-task to %s", task_file)

    return SpawnCoordinatorResult(
        slug=slug,
        worktree=str(worktree_path),
        host_worktree=str(host_worktree_path),
        branch=branch,
        agent_task=agent_task_content,
    )


class ConductorHistoryEntry(BaseModel):
    """One entry in the conductor spawn history returned by ``GET /control/conductor-history``."""

    wave_id: str
    worktree: str
    host_worktree: str
    started_at: str
    status: str  # "active" (worktree exists) | "completed" (worktree removed)


@router.get("/control/conductor-history", tags=["control"])
async def conductor_history() -> list[ConductorHistoryEntry]:
    """Return the last 5 conductor spawns with their current status.

    Status is ``"active"`` when the worktree directory is still present on disk
    and ``"completed"`` once it has been removed.  Returns ``[]`` when the DB
    is unavailable rather than raising an error — callers should treat an empty
    list as "no history yet."
    """
    from agentception.db.queries import get_conductor_history

    entries = await get_conductor_history(limit=5)
    return [ConductorHistoryEntry(**e) for e in entries]


@router.post("/control/spawn-conductor", tags=["control"])
async def spawn_conductor(body: SpawnConductorRequest) -> SpawnConductorResult:
    """Seed a conductor worktree that orchestrates a multi-phase wave.

    Creates a git worktree and writes an ``.agent-task`` file that tells a
    Cursor background agent to run as a conductor across the provided phases.
    The conductor will spawn sub-agents for every unclaimed issue in each phase
    without requiring manual intervention per-issue.

    This endpoint only creates the worktree and task file.  The caller
    (AgentCeption UI) instructs the user to open the returned
    ``host_worktree`` path in Cursor to start the conductor agent.

    Raises
    ------
    HTTP 409
        When a worktree with the same wave_id already exists.
    HTTP 422
        When ``phases`` is empty.
    HTTP 500
        When ``git worktree add`` fails.
    """
    from agentception.db.persist import persist_wave_start

    if not body.phases:
        raise HTTPException(status_code=422, detail="phases must not be empty")

    wave_id = f"conductor-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    slug = wave_id
    branch = f"feat/{slug}"
    worktree_path = settings.worktrees_dir / slug
    host_worktree_path = settings.host_worktrees_dir / slug

    if worktree_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Worktree '{slug}' already exists — wait a second and retry.",
        )

    repo_dir = str(settings.repo_dir)

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "add", "-b", branch,
        str(worktree_path), "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(
                f"git worktree add failed (exit {proc.returncode}): "
                f"{stderr.decode().strip()!r}"
            ),
        )

    lock_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "lock", str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await lock_proc.communicate()  # best-effort — non-fatal if it fails

    logger.info(
        "✅ Created conductor worktree %s on branch %s (phases=%s)",
        worktree_path,
        branch,
        body.phases,
    )

    agent_task_content = _build_conductor_task(
        wave_id=wave_id,
        phases=body.phases,
        org=body.org,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
    )
    task_file = worktree_path / ".agent-task"
    task_file.write_text(agent_task_content, encoding="utf-8")
    logger.info("✅ Wrote conductor .agent-task to %s", task_file)

    await persist_wave_start(wave_id, ",".join(body.phases), "conductor")

    return SpawnConductorResult(
        wave_id=wave_id,
        worktree=str(worktree_path),
        host_worktree=str(host_worktree_path),
        branch=branch,
        agent_task=agent_task_content,
    )
