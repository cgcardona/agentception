from __future__ import annotations

"""Run lifecycle API routes — agent callbacks and run management.

Two audiences:

1. **The AgentCeption Coordinator / Dispatcher** — ``GET /api/runs/pending``
   exposes the launch queue; ``POST /api/runs/{run_id}/acknowledge``
   atomically claims a run; ``POST /api/runs/{parent_run_id}/children``
   lets any manager agent create a child node atomically.

2. **Running agents** — ``POST /api/runs/{run_id}/step|blocker|decision|done``
   let agents push structured lifecycle events back to AgentCeption.

3. **Ship UI operators** — ``POST /api/runs/{run_id}/message`` saves a user
   message to the agent's transcript; ``POST /api/runs/{run_id}/stop``
   marks a run DONE from the inspector panel.

See ``docs/agent-tree-protocol.md`` for the full tier spec.
"""

import asyncio
import datetime
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from typing import Literal

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentMessage, ACAgentRun
from agentception.db.persist import acknowledge_agent_run, persist_agent_event
from agentception.db.queries import get_agent_run_teardown, get_pending_launches
from agentception.services.spawn_child import (
    SpawnChildError,
    Tier,
    spawn_child,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# GET /api/runs/pending — Dispatcher reads this to know what to spawn
# ---------------------------------------------------------------------------


@router.get("/pending")
async def list_pending_launches() -> dict[str, object]:
    """Return all runs waiting to be claimed by the Dispatcher.

    The AgentCeption Dispatcher calls this once at startup to discover what
    the UI has queued.  Each item includes the run_id, role, issue number,
    and host-side worktree path so the Dispatcher can spawn the right agent
    at the right level of the tree (leaf worker, VP, or CTO).
    """
    launches = await get_pending_launches()
    return {"pending": launches, "count": len(launches)}


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/acknowledge — atomically claim a pending run
# ---------------------------------------------------------------------------


@router.post("/{run_id}/acknowledge")
async def acknowledge_launch(run_id: str) -> dict[str, object]:
    """Atomically claim a pending run before spawning its Task agent.

    The Dispatcher calls this immediately before it spawns the Task so the
    run cannot be double-claimed if two Dispatchers run concurrently.
    Transitions the run from ``pending_launch`` → ``implementing``.

    Returns ``{"ok": true}`` on success or ``{"ok": false, "reason": "..."}``
    when the run was not found or already claimed (idempotency guard).
    """
    ok = await acknowledge_agent_run(run_id)
    if not ok:
        return {"ok": False, "reason": f"Run {run_id!r} not found or not in pending_launch state"}
    logger.info("✅ acknowledge_launch: %s claimed", run_id)
    return {"ok": True, "run_id": run_id}


# ---------------------------------------------------------------------------
# POST /api/runs/{parent_run_id}/children — spawn a child node
# ---------------------------------------------------------------------------


class SpawnChildRequest(BaseModel):
    """Request body for ``POST /api/runs/{parent_run_id}/children``."""

    role: str
    """Child's role slug (e.g. ``"engineering-coordinator"``, ``"python-developer"``)."""
    tier: Tier
    """Behavioral execution tier: ``"executive"`` | ``"coordinator"`` | ``"engineer"`` | ``"reviewer"``."""
    org_domain: str | None = None
    """Organisational slot for UI hierarchy (``"c-suite"`` | ``"engineering"`` | ``"qa"``)."""
    scope_type: Literal["label", "issue", "pr"]
    """``"label"``, ``"issue"``, or ``"pr"``."""
    scope_value: str
    """Label string, issue number, or PR number (as string)."""
    gh_repo: str
    """``"owner/repo"`` string."""
    issue_body: str = ""
    """Issue body for COGNITIVE_ARCH skill extraction (issue-scoped children)."""
    issue_title: str = ""
    """Issue title written to ISSUE_TITLE field."""
    skills_hint: list[str] | None = None
    """Explicit skill list; bypasses keyword extraction when provided."""


class SpawnChildResponse(BaseModel):
    """Successful response from ``POST /api/runs/{parent_run_id}/children``."""

    run_id: str
    host_worktree_path: str
    worktree_path: str
    tier: str
    org_domain: str | None = None
    role: str
    cognitive_arch: str
    agent_task_path: str
    scope_type: str
    scope_value: str
    status: str = "implementing"


@router.post("/{parent_run_id}/children", response_model=SpawnChildResponse)
async def spawn_child_node(
    parent_run_id: str, req: SpawnChildRequest
) -> SpawnChildResponse:
    """Atomically create a child node in the agent tree.

    Any manager agent (CTO, coordinator, or future tier) calls this endpoint
    to create a child with a worktree, ``.agent-task``, DB record, and
    auto-acknowledgement — all in a single atomic operation.

    The caller receives ``host_worktree_path`` and ``run_id``, then
    immediately fires a Task tool call with the briefing:

        "Read your .agent-task file at {host_worktree_path}/.agent-task
         and follow the instructions for your role."

    Raises:
        HTTPException 422: Invalid ``scope_type`` value.
        HTTPException 500: Worktree creation or file I/O failure.
    """
    try:
        result = await spawn_child(
            parent_run_id=parent_run_id,
            role=req.role,
            tier=req.tier,
            org_domain=req.org_domain,
            scope_type=req.scope_type,
            scope_value=req.scope_value,
            gh_repo=req.gh_repo,
            issue_body=req.issue_body,
            issue_title=req.issue_title,
            skills_hint=req.skills_hint,
        )
    except SpawnChildError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SpawnChildResponse(**result.to_dict(), status="implementing")


# ---------------------------------------------------------------------------
# Agent callbacks — POST /api/runs/{run_id}/step|blocker|decision|done
# ---------------------------------------------------------------------------


class StepReport(BaseModel):
    """Body for ``POST /api/runs/{run_id}/step``."""

    issue_number: int
    step_name: str


class BlockerReport(BaseModel):
    """Body for ``POST /api/runs/{run_id}/blocker``."""

    issue_number: int
    description: str


class DecisionReport(BaseModel):
    """Body for ``POST /api/runs/{run_id}/decision``."""

    issue_number: int
    decision: str
    rationale: str


class DoneReport(BaseModel):
    """Body for ``POST /api/runs/{run_id}/done``."""

    issue_number: int
    pr_url: str
    summary: str = ""


@router.post("/{run_id}/step")
async def report_step(run_id: str, req: StepReport) -> dict[str, object]:
    """Agent reports starting a named execution step."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="step_start",
        payload={"step": req.step_name},
        agent_run_id=run_id,
    )
    logger.info("✅ report_step: run=%r issue=%d step=%r", run_id, req.issue_number, req.step_name)
    return {"ok": True}


@router.post("/{run_id}/blocker")
async def report_blocker(run_id: str, req: BlockerReport) -> dict[str, object]:
    """Agent reports being blocked."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="blocker",
        payload={"description": req.description},
        agent_run_id=run_id,
    )
    logger.warning(
        "⚠️ report_blocker: run=%r issue=%d — %s", run_id, req.issue_number, req.description
    )
    return {"ok": True}


@router.post("/{run_id}/decision")
async def report_decision(run_id: str, req: DecisionReport) -> dict[str, object]:
    """Agent records an architectural decision."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="decision",
        payload={"decision": req.decision, "rationale": req.rationale},
        agent_run_id=run_id,
    )
    logger.info(
        "✅ report_decision: run=%r issue=%d decision=%r", run_id, req.issue_number, req.decision
    )
    return {"ok": True}


async def _teardown_agent_worktree(run_id: str) -> None:
    """Remove the worktree and delete the remote branch for a completed agent run.

    Called non-blocking from ``report_done`` — errors are logged but never
    propagated so a cleanup failure cannot break the agent's done response.
    """
    teardown = await get_agent_run_teardown(run_id)
    if teardown is None:
        logger.warning("⚠️  _teardown_agent_worktree: no DB row for run_id=%r", run_id)
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
            logger.info("✅ _teardown: removed worktree %s", worktree_path)
        else:
            logger.warning("⚠️  _teardown: worktree remove failed: %s", stderr.decode().strip())

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
            logger.info("✅ _teardown: deleted remote branch %r", branch)
        else:
            logger.info(
                "ℹ️  _teardown: remote branch %r not deleted (may already be gone): %s",
                branch,
                push_stderr.decode().strip(),
            )


@router.post("/{run_id}/done")
async def report_done(run_id: str, req: DoneReport) -> dict[str, object]:
    """Agent reports completion, links the PR, and tears down its worktree.

    The worktree removal and remote branch deletion run as a background task
    so the agent receives an immediate ``{"ok": True}`` response and is not
    blocked waiting for git operations to complete.
    """
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="done",
        payload={"pr_url": req.pr_url, "summary": req.summary},
        agent_run_id=run_id,
    )
    logger.info("✅ report_done: run=%r issue=%d pr_url=%r", run_id, req.issue_number, req.pr_url)
    asyncio.create_task(
        _teardown_agent_worktree(run_id),
        name=f"teardown-{run_id}",
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/message — operator sends a message to an agent
# ---------------------------------------------------------------------------


class _AgentMessageBody(BaseModel):
    content: str


@router.post("/{run_id}/message", response_model=None)
async def post_agent_message(run_id: str, body: _AgentMessageBody) -> Response:
    """Append a user message to an agent run's transcript.

    The message is stored in ``agent_messages`` with ``role='user'`` and
    immediately picked up by the inspector's SSE stream on the next 2-second
    poll.

    Returns 404 if the run_id is not found.
    """
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must be non-empty")

    try:
        async with get_session() as session:
            run_exists = await session.scalar(
                select(func.count()).select_from(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            if not run_exists:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            seq_result = await session.scalar(
                select(func.coalesce(func.max(ACAgentMessage.sequence_index), -1)).where(
                    ACAgentMessage.agent_run_id == run_id
                )
            )
            next_seq = int(seq_result) + 1 if seq_result is not None else 0

            session.add(
                ACAgentMessage(
                    agent_run_id=run_id,
                    role="user",
                    content=content,
                    sequence_index=next_seq,
                    recorded_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ post_agent_message failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save message") from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/cancel — abort a pending_launch before dispatch
# ---------------------------------------------------------------------------


@router.post("/{run_id}/cancel", response_model=None)
async def cancel_pending_run(run_id: str) -> Response:
    """Cancel a queued run before it is claimed by the Dispatcher.

    Only works on runs still in ``pending_launch`` state — once the Dispatcher
    claims a run (transitions it to ``implementing``) it cannot be cancelled
    through this endpoint.

    Returns 204 on success, 409 if already claimed/not pending.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
            if run.status != "pending_launch":
                raise HTTPException(
                    status_code=409,
                    detail=f"Run '{run_id}' is '{run.status}' — only pending_launch runs can be cancelled",
                )
            run.status = "cancelled"
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ cancel_pending_run failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to cancel run") from exc

    logger.info("✅ cancel_pending_run: %s cancelled", run_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/stop — mark a run as done from the inspector
# ---------------------------------------------------------------------------


@router.post("/{run_id}/stop", response_model=None)
async def stop_agent(run_id: str) -> Response:
    """Mark an agent run as DONE so the board no longer shows it as active.

    Does not remove the worktree (use the control panel kill action for that).
    This is a lightweight "I'm done watching this" action that removes the
    run from the active-agent display so a fresh agent can be dispatched.

    Returns 404 if the run_id is not found.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            run.status = "done"
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ stop_agent failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to stop agent") from exc

    return Response(status_code=204)
