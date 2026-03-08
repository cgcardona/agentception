from __future__ import annotations

"""Ad-hoc agent run endpoint.

Provides a single endpoint that creates a fully self-contained agent run
without requiring a GitHub issue, a wave, or a pre-written ``.agent-task``
file.  The caller supplies a role, an optional cognitive figure, and a plain-
language task description.  The endpoint:

1. Generates a UUID run ID.
2. Creates a git worktree from ``origin/dev`` at ``worktrees_dir / run_id``.
3. Inserts an ``ACAgentRun`` DB row with ``status = "implementing"`` and the
   task context stored inline (``task_description`` column).
4. Fires :func:`~agentception.services.agent_loop.run_agent_loop` as a
   ``BackgroundTask`` and returns ``202 Accepted`` immediately.

This is the entry point for the Cursor-replacement loop: no Cursor session,
no file paste, no manual worktree setup — just a POST and an agent running.

Endpoint
--------
POST /api/runs/adhoc
    Body: ``AdhocRunRequest`` JSON.
    Returns: ``{ "ok": true, "run_id": "...", "worktree_path": "..." }``
"""

import asyncio
import datetime
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.services.agent_loop import run_agent_loop
from agentception.services.cognitive_arch import ROLE_DEFAULT_FIGURE, _resolve_cognitive_arch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["agent-run"])


class AdhocRunRequest(BaseModel):
    """Request body for POST /api/runs/adhoc."""

    role: str
    """Role slug — must exist in ``.agentception/roles/``.

    Examples: ``"python-developer"``, ``"test-engineer"``, ``"architect"``.
    """

    task_description: str
    """Plain-language description of what the agent should do.

    This is injected verbatim as the first user message in the agent loop.
    Be specific: include the target files, the expected output, and any
    constraints.  The agent reads the codebase and acts autonomously from here.
    """

    figure: str | None = None
    """Cognitive figure slug override (e.g. ``"guido_van_rossum"``).

    When omitted, the default figure for the role is used from
    ``ROLE_DEFAULT_FIGURE``.  Passing an explicit figure lets callers
    experiment with different cognitive identities for the same role.
    """

    base_branch: str = "origin/dev"
    """Git ref to branch the worktree from.  Defaults to ``origin/dev``."""


class AdhocRunResponse(BaseModel):
    """Successful response from POST /api/runs/adhoc."""

    ok: bool
    run_id: str
    worktree_path: str
    cognitive_arch: str


@router.post("/adhoc", status_code=202, response_model=AdhocRunResponse)
async def create_adhoc_run(
    req: AdhocRunRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Create a self-contained agent run from an inline task description.

    The run bypasses the GitHub-issue dispatch pipeline entirely.  The agent
    loop receives the task description directly in its first message — no
    ``.agent-task`` file indirection required.

    Returns 202 immediately.  Monitor progress via the build dashboard or
    ``GET /api/runs/{run_id}``.
    """
    run_id = f"adhoc-{uuid.uuid4().hex[:12]}"
    worktree_path = settings.worktrees_dir / run_id
    branch_name = f"adhoc/{run_id}"

    figure = req.figure or ROLE_DEFAULT_FIGURE.get(req.role, "hopper")
    cognitive_arch = _resolve_cognitive_arch(
        issue_body="",
        role=req.role,
        figure_override=figure,
    )

    await _create_worktree(worktree_path, branch_name, req.base_branch, run_id)

    await _insert_run(
        run_id=run_id,
        role=req.role,
        cognitive_arch=cognitive_arch,
        worktree_path=worktree_path,
        branch=branch_name,
        task_description=req.task_description,
    )

    background_tasks.add_task(run_agent_loop, run_id)
    logger.info(
        "✅ adhoc run dispatched — run_id=%s role=%s arch=%s",
        run_id,
        req.role,
        cognitive_arch,
    )

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "run_id": run_id,
            "worktree_path": str(worktree_path),
            "cognitive_arch": cognitive_arch,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_worktree(
    worktree_path: Path,
    branch_name: str,
    base_ref: str,
    run_id: str,
) -> None:
    """Create a git worktree at *worktree_path* branching off *base_ref*.

    Raises ``HTTPException(500)`` when git fails so the caller can surface a
    clean error without creating a dangling DB row.
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        base_ref,
        cwd=str(settings.repo_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ _create_worktree failed for run_id=%s: %s", run_id, err)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create git worktree: {err}",
        )
    logger.info("✅ worktree created — %s", worktree_path)


async def _insert_run(
    *,
    run_id: str,
    role: str,
    cognitive_arch: str,
    worktree_path: Path,
    branch: str,
    task_description: str,
) -> None:
    """Insert an ``ACAgentRun`` row directly into ``implementing`` state.

    Ad-hoc runs skip ``pending_launch`` — the loop is fired immediately by
    the route handler, so there is no dispatcher handoff to wait for.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    async with get_session() as session:
        run = ACAgentRun(
            id=run_id,
            wave_id=None,
            issue_number=None,
            pr_number=None,
            branch=branch,
            worktree_path=str(worktree_path),
            role=role,
            status="implementing",
            attempt_number=0,
            spawn_mode=None,
            batch_id=None,
            cognitive_arch=cognitive_arch,
            tier="worker",
            org_domain="engineering",
            parent_run_id=None,
            spawned_at=now,
            last_activity_at=now,
            completed_at=None,
            task_description=task_description,
        )
        session.add(run)
        await session.commit()
    logger.info("✅ ACAgentRun inserted — run_id=%s status=implementing", run_id)
