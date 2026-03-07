from __future__ import annotations

"""MCP Build Command tools — explicit run lifecycle state transitions.

Every function in this module represents a state transition in the run state
machine.  Functions are grouped by audience:

Dispatcher
----------
- ``build_claim_run``       — pending_launch → implementing (was: build_acknowledge_run)
- ``build_spawn_child_run`` — create child worktree + DB record (was: build_spawn_child)

Engineers
---------
- ``build_complete_run``    — implementing → completed (was: split from build_report_done)
- ``build_teardown_worktree`` — clean up worktree after completion

Rules
-----
- These tools change run state.  They never append telemetry events.
- All state transitions are validated server-side (see db/persist.py).
- Each tool returns a structured dict — never prose.
"""

import asyncio
import logging

from agentception.db.persist import (
    acknowledge_agent_run,
    complete_agent_run,
    persist_agent_event,
)
from agentception.db.queries import get_pending_launches
from agentception.services.spawn_child import ScopeType, SpawnChildError, Tier, spawn_child
from agentception.services.teardown import teardown_agent_worktree

logger = logging.getLogger(__name__)


async def build_claim_run(run_id: str) -> dict[str, object]:
    """Atomically claim a pending run before spawning its Task agent.

    Transitions the run from ``pending_launch`` → ``implementing``.  Call this
    immediately before firing the Task so the run cannot be double-claimed by a
    concurrent Dispatcher.

    Was: ``build_acknowledge_run``.

    Args:
        run_id: The ``run_id`` returned by ``query_pending_runs``
                (e.g. ``"label-cognitive-arch-propagation-7352b9"``).

    Returns:
        ``{"ok": True, "run_id": run_id, "previous_state": "pending_launch"}`` on
        success, or ``{"ok": False, "reason": "..."}`` when the run is not found
        or was already claimed by another Dispatcher.
    """
    ok = await acknowledge_agent_run(run_id)
    if not ok:
        logger.warning(
            "⚠️ build_claim_run: %r not found or already claimed — skipping",
            run_id,
        )
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or not in pending_launch state",
        }
    logger.info("✅ build_claim_run: %r claimed", run_id)
    return {"ok": True, "run_id": run_id, "previous_state": "pending_launch"}


async def build_spawn_child_run(
    parent_run_id: str,
    role: str,
    tier: str,
    scope_type: str,
    scope_value: str,
    gh_repo: str,
    org_domain: str = "",
    issue_body: str = "",
    issue_title: str = "",
    skills_hint: list[str] | None = None,
    coord_fingerprint: str | None = None,
    cognitive_arch: str = "",
) -> dict[str, object]:
    """Create a child agent node in the tree and return its worktree path.

    Any coordinator agent calls this tool to atomically spawn a child.
    The tool creates the worktree, writes the ``.agent-task`` file (with
    TIER, ORG_DOMAIN if provided, COGNITIVE_ARCH, SCOPE_TYPE,
    SCOPE_VALUE, PARENT_RUN_ID, and all required fields), registers the DB
    record, and auto-acknowledges the run so the caller can immediately fire
    a Task call.

    Was: ``build_spawn_child``.

    Args:
        parent_run_id:  ``run_id`` of the calling agent (lineage tracking).
        role:           Child's role slug (e.g. ``"engineering-coordinator"``).
        tier:           Behavioral execution tier — ``"executive"``, ``"coordinator"``,
                        ``"engineer"``, or ``"reviewer"``.
        scope_type:     ``"label"``, ``"issue"``, or ``"pr"``.
        scope_value:    Label string, or issue/PR number as a string.
        gh_repo:        ``"owner/repo"`` string.
        org_domain:     Organisational slot for UI hierarchy (``"c-suite"``,
                        ``"engineering"``, ``"qa"``).
        issue_body:     Issue body for COGNITIVE_ARCH skill extraction.
        issue_title:    Issue title written to ISSUE_TITLE field.
        skills_hint:    Explicit skill override list for COGNITIVE_ARCH.
        coord_fingerprint: The spawning coordinator's fingerprint string.
        cognitive_arch: When provided, forward this exact arch string to the child.

    Returns:
        On success: ``{"ok": True, "child_run_id": ..., "worktree_path": ...,
                       "tier": ..., "org_domain": ..., "role": ..., "cognitive_arch": ...}``
        On failure: ``{"ok": False, "error": "<reason>"}``
    """
    if tier == "executive":
        typed_tier: Tier = "executive"
    elif tier == "coordinator":
        typed_tier = "coordinator"
    elif tier == "engineer":
        typed_tier = "engineer"
    elif tier == "reviewer":
        typed_tier = "reviewer"
    else:
        return {
            "ok": False,
            "error": f"tier must be executive/coordinator/engineer/reviewer, got {tier!r}",
        }

    if scope_type == "label":
        scope: ScopeType = "label"
    elif scope_type == "issue":
        scope = "issue"
    elif scope_type == "pr":
        scope = "pr"
    else:
        return {
            "ok": False,
            "error": f"scope_type must be label/issue/pr, got {scope_type!r}",
        }

    domain: str | None = org_domain if org_domain else None

    try:
        result = await spawn_child(
            parent_run_id=parent_run_id,
            role=role,
            tier=typed_tier,
            org_domain=domain,
            scope_type=scope,
            scope_value=scope_value,
            gh_repo=gh_repo,
            issue_body=issue_body,
            issue_title=issue_title,
            skills_hint=skills_hint,
            coord_fingerprint=coord_fingerprint,
            cognitive_arch=cognitive_arch if cognitive_arch else None,
        )
    except SpawnChildError as exc:
        logger.error("❌ build_spawn_child_run failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info(
        "✅ build_spawn_child_run: spawned child_run_id=%r role=%r tier=%r org_domain=%r scope=%s:%s",
        result.run_id, result.role, result.tier, result.org_domain,
        result.scope_type, result.scope_value,
    )
    return {
        "ok": True,
        "child_run_id": result.run_id,
        "worktree_path": result.host_worktree_path,
        "tier": result.tier,
        "org_domain": result.org_domain,
        "role": result.role,
        "cognitive_arch": result.cognitive_arch,
        "agent_task_path": result.agent_task_path,
        "scope_type": result.scope_type,
        "scope_value": result.scope_value,
        "status": "implementing",
    }


async def build_complete_run(
    issue_number: int,
    pr_url: str,
    summary: str = "",
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record that the agent has finished work and transition to completed.

    Persists the ``done`` event (linking the PR and updating workflow state),
    then transitions the run to ``completed``.  Does NOT tear down the worktree
    — call ``build_teardown_worktree`` explicitly after this if cleanup is needed.

    Was: part of ``build_report_done``.  Teardown is now a separate explicit
    command so orchestration layers can control when cleanup happens.

    Args:
        issue_number: GitHub issue number the agent worked on.
        pr_url: Full URL of the opened pull request.
        summary: Optional one-sentence description of what was done.
        agent_run_id: Run ID used to transition the run state.

    Returns:
        ``{"ok": True, "event": "done", "status": "completed"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="done",
        payload={"pr_url": pr_url, "summary": summary},
        agent_run_id=agent_run_id,
    )

    if agent_run_id:
        ok = await complete_agent_run(agent_run_id)
        if not ok:
            logger.warning(
                "⚠️ build_complete_run: complete_agent_run returned False for run_id=%r "
                "(run may not be in implementing state — event still recorded)",
                agent_run_id,
            )

    logger.info(
        "✅ build_complete_run: issue=%d pr_url=%r run_id=%r",
        issue_number, pr_url, agent_run_id,
    )
    return {"ok": True, "event": "done", "status": "completed"}


async def build_teardown_worktree(agent_run_id: str) -> dict[str, object]:
    """Clean up the git worktree for a completed or stopped run.

    Fires ``teardown_agent_worktree`` as a background task so the caller
    receives an immediate ack while the actual cleanup runs asynchronously.
    Teardown removes the git worktree, prunes refs, deletes the remote branch,
    and deletes the local branch.

    Call this after ``build_complete_run``.  The Dispatcher or orchestration
    layer is responsible for deciding when teardown happens — engineers should
    not call this directly.

    Was: the teardown side-effect hidden inside ``build_report_done``.

    Args:
        agent_run_id: The run ID of the completed agent (must have a worktree).

    Returns:
        ``{"ok": True, "run_id": agent_run_id, "teardown": "queued"}``
    """
    if not agent_run_id:
        return {"ok": False, "reason": "agent_run_id is required"}

    asyncio.create_task(
        teardown_agent_worktree(agent_run_id),
        name=f"teardown-{agent_run_id}",
    )
    logger.info("🧹 build_teardown_worktree: teardown queued for run_id=%r", agent_run_id)
    return {"ok": True, "run_id": agent_run_id, "teardown": "queued"}
