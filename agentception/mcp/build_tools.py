from __future__ import annotations

"""AgentCeption MCP tools for the Build phase.

These four tools are called by running agents to push structured lifecycle
events back to AgentCeption.  They complement the passive transcript reader
(which captures raw thinking messages) by letting agents proactively signal
intent: what step they're on, when they're blocked, important decisions, and
when they finish.

All four functions are async — they write to ``ac_agent_events`` via the
persist layer and return a lightweight ack dict.
"""

import logging

from agentception.db.persist import persist_agent_event
from agentception.db.queries import get_pending_launches
from agentception.services.spawn_child import NodeType, ScopeType, SpawnChildError, spawn_child

logger = logging.getLogger(__name__)


async def build_get_pending_launches() -> dict[str, object]:
    """Return all pending launch records from the AgentCeption DB.

    The Dispatcher calls this once to discover what the UI has queued.
    Each item in ``pending`` contains:
      - ``run_id``             — worktree id (e.g. "issue-1234")
      - ``issue_number``       — GitHub issue number
      - ``role``               — role slug (e.g. "cto", "python-developer")
      - ``branch``             — git branch to work on
      - ``host_worktree_path`` — full path on the HOST filesystem
      - ``batch_id``           — batch fingerprint

    The ``role`` field is the tree entry point — the Dispatcher spawns
    whatever role was assigned. A leaf worker runs directly; a coordinator
    reads its role file and spawns its own children.
    """
    logger.warning("🔍 build_get_pending_launches: querying DB for pending launches")
    launches = await get_pending_launches()
    logger.warning(
        "🔍 build_get_pending_launches: got %d row(s) from DB",
        len(launches),
    )
    for i, launch in enumerate(launches):
        logger.warning(
            "🔍   [%d] run_id=%r role=%r status=pending_launch "
            "host_worktree_path=%r branch=%r",
            i,
            launch.get("run_id"),
            launch.get("role"),
            launch.get("host_worktree_path"),
            launch.get("branch"),
        )
    return {"pending": launches, "count": len(launches)}


async def build_report_step(
    issue_number: int,
    step_name: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record that the agent is starting a named execution step.

    Args:
        issue_number: GitHub issue number the agent is working on.
        step_name: Human-readable step label (e.g. "Reading codebase").
        agent_run_id: Optional worktree id (e.g. "issue-938").

    Returns:
        ``{"ok": True, "event": "step_start"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="step_start",
        payload={"step": step_name},
        agent_run_id=agent_run_id,
    )
    logger.info("✅ build_report_step: issue=%d step=%r", issue_number, step_name)
    return {"ok": True, "event": "step_start"}


async def build_report_blocker(
    issue_number: int,
    description: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record that the agent is blocked and cannot proceed without help.

    Args:
        issue_number: GitHub issue number the agent is working on.
        description: What is blocking the agent.
        agent_run_id: Optional worktree id.

    Returns:
        ``{"ok": True, "event": "blocker"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="blocker",
        payload={"description": description},
        agent_run_id=agent_run_id,
    )
    logger.warning("⚠️ build_report_blocker: issue=%d — %s", issue_number, description)
    return {"ok": True, "event": "blocker"}


async def build_report_decision(
    issue_number: int,
    decision: str,
    rationale: str,
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record a significant architectural or implementation decision.

    Args:
        issue_number: GitHub issue number the agent is working on.
        decision: One-sentence description of the decision made.
        rationale: Why this decision was made.
        agent_run_id: Optional worktree id.

    Returns:
        ``{"ok": True, "event": "decision"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="decision",
        payload={"decision": decision, "rationale": rationale},
        agent_run_id=agent_run_id,
    )
    logger.info(
        "✅ build_report_decision: issue=%d decision=%r", issue_number, decision
    )
    return {"ok": True, "event": "decision"}


async def build_spawn_child(
    parent_run_id: str,
    role: str,
    node_type: str,
    scope_type: str,
    scope_value: str,
    gh_repo: str,
    logical_tier: str = "",
    issue_body: str = "",
    issue_title: str = "",
    skills_hint: list[str] | None = None,
    coord_fingerprint: str | None = None,
) -> dict[str, object]:
    """Create a child agent node in the tree and return its worktree path.

    Any coordinator agent calls this tool to atomically spawn a child.
    The tool creates the worktree, writes the ``.agent-task`` file (with
    NODE_TYPE, LOGICAL_TIER if provided, COGNITIVE_ARCH, SCOPE_TYPE,
    SCOPE_VALUE, PARENT_RUN_ID, and all required fields), registers the DB
    record, and auto-acknowledges the run so the caller can immediately fire
    a Task call.

    Args:
        parent_run_id:  ``run_id`` of the calling agent (lineage tracking).
        role:           Child's role slug (e.g. ``"engineering-coordinator"``).
        node_type:      ``"coordinator"`` if the child surveys a scope and spawns
                        its own children; ``"leaf"`` if it works one issue/PR.
                        The caller always knows which type it is spawning.
        scope_type:     ``"label"``, ``"issue"``, or ``"pr"``.
        scope_value:    Label string, or issue/PR number as a string.
        gh_repo:        ``"owner/repo"`` string.
        logical_tier:   Org domain for UI visualisation (e.g. ``"qa"``,
                        ``"engineering"``, ``"c-suite"``).  Pass ``"qa"`` when
                        chain-spawning a PR reviewer so the dashboard places it
                        under the QA branch.  Optional — omit or pass ``""`` to
                        leave the field unset.
        issue_body:         Issue body for COGNITIVE_ARCH skill extraction.
        issue_title:        Issue title written to ISSUE_TITLE field.
        skills_hint:        Explicit skill override list for COGNITIVE_ARCH.
        coord_fingerprint:  The spawning coordinator's fingerprint string, written
                            as COORD_FINGERPRINT in the child's .agent-task.

    Returns:
        On success: ``{"ok": True, "run_id": ..., "host_worktree_path": ...,
                       "node_type": ..., "logical_tier": ..., "role": ..., "cognitive_arch": ...}``
        On failure: ``{"ok": False, "error": "<reason>"}``
    """
    if node_type == "coordinator":
        nt: NodeType = "coordinator"
    elif node_type == "leaf":
        nt = "leaf"
    else:
        return {"ok": False, "error": f"node_type must be coordinator/leaf, got {node_type!r}"}

    if scope_type == "label":
        scope: ScopeType = "label"
    elif scope_type == "issue":
        scope = "issue"
    elif scope_type == "pr":
        scope = "pr"
    else:
        return {"ok": False, "error": f"scope_type must be label/issue/pr, got {scope_type!r}"}

    tier: str | None = logical_tier if logical_tier else None

    try:
        result = await spawn_child(
            parent_run_id=parent_run_id,
            role=role,
            node_type=nt,
            logical_tier=tier,
            scope_type=scope,
            scope_value=scope_value,
            gh_repo=gh_repo,
            issue_body=issue_body,
            issue_title=issue_title,
            skills_hint=skills_hint,
            coord_fingerprint=coord_fingerprint,
        )
    except SpawnChildError as exc:
        logger.error("❌ build_spawn_child failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info(
        "✅ build_spawn_child: spawned run_id=%r role=%r node_type=%r logical_tier=%r scope=%s:%s",
        result.run_id, result.role, result.node_type, result.logical_tier,
        result.scope_type, result.scope_value,
    )
    return {
        "ok": True,
        "run_id": result.run_id,
        "host_worktree_path": result.host_worktree_path,
        "node_type": result.node_type,
        "logical_tier": result.logical_tier,
        "role": result.role,
        "cognitive_arch": result.cognitive_arch,
        "agent_task_path": result.agent_task_path,
        "scope_type": result.scope_type,
        "scope_value": result.scope_value,
        "status": "implementing",
    }


async def build_report_done(
    issue_number: int,
    pr_url: str,
    summary: str = "",
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """Record that the agent has finished work and opened a pull request.

    Args:
        issue_number: GitHub issue number the agent worked on.
        pr_url: Full URL of the opened pull request.
        summary: Optional one-sentence description of what was done.
        agent_run_id: Optional worktree id.

    Returns:
        ``{"ok": True, "event": "done"}``
    """
    await persist_agent_event(
        issue_number=issue_number,
        event_type="done",
        payload={"pr_url": pr_url, "summary": summary},
        agent_run_id=agent_run_id,
    )
    logger.info(
        "✅ build_report_done: issue=%d pr_url=%r", issue_number, pr_url
    )
    return {"ok": True, "event": "done"}
