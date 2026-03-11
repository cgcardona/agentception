from __future__ import annotations

"""MCP Query tools — read-only state inspection for agents.

Every function in this module is a pure read — no state transitions, no
side effects.  Agents use these to reconstruct state on startup, inspect
the run tree, and determine what work is available.

Rules
-----
- Never write to DB.
- Never change run state.
- Return structured dicts — never prose.
- Degrade gracefully: on DB error return ``{"ok": False, "error": "..."}``
  so an agent can retry rather than crash.
"""

import logging

from agentception.db.queries import (
    check_db_reachable,
    get_active_runs,
    get_agent_events_tail,
    get_children_by_parent_id,
    get_latest_active_batch_id,
    get_pending_launches,
    get_run_by_id,
    get_run_context,
    get_run_status_counts,
    get_run_tree_by_batch_id,
)

logger = logging.getLogger(__name__)


async def query_pending_runs() -> dict[str, object]:
    """Return all pending launch records from the AgentCeption DB.

    The Dispatcher calls this once to discover what the UI has queued.
    Each item in ``pending`` contains:
      - ``run_id``             — worktree id (e.g. "issue-1234")
      - ``issue_number``       — GitHub issue number
      - ``role``               — role slug (e.g. "cto", "developer")
      - ``branch``             — git branch to work on
      - ``host_worktree_path`` — full path on the HOST filesystem
      - ``batch_id``           — batch fingerprint

    Was: ``build_get_pending_launches``.

    Returns:
        ``{"pending": [...], "count": N}``
    """
    logger.debug("🔍 query_pending_runs: querying DB for pending launches")
    launches = await get_pending_launches()
    logger.debug("🔍 query_pending_runs: got %d row(s) from DB", len(launches))
    for i, launch in enumerate(launches):
        logger.debug(
            "🔍   [%d] run_id=%r role=%r host_worktree_path=%r branch=%r",
            i,
            launch.get("run_id"),
            launch.get("role"),
            launch.get("host_worktree_path"),
            launch.get("branch"),
        )
    return {"pending": launches, "count": len(launches)}


async def query_run(run_id: str) -> dict[str, object]:
    """Return lightweight metadata for a single run.

    Agents call this on startup to determine their current state and decide
    whether to claim, resume, complete, or block.

    Args:
        run_id: The run ID to look up.

    Returns:
        ``{"ok": True, "run": {...}}`` when found, or
        ``{"ok": False, "error": "not found"}`` when the run does not exist.
    """
    row = await get_run_by_id(run_id)
    if row is None:
        logger.warning("🔍 query_run: run_id=%r not found", run_id)
        return {"ok": False, "error": f"Run {run_id!r} not found"}
    logger.info("✅ query_run: found run_id=%r status=%r", run_id, row["status"])
    return {"ok": True, "run": dict(row)}


async def query_run_context(run_id: str) -> dict[str, object]:
    """Return the full task context for a single run.

    Unlike ``query_run``, this includes ``cognitive_arch`` and
    ``task_description`` — everything an agent needs to understand its
    complete assignment.  Served as the ``ac://runs/{run_id}/context``
    MCP resource and used by the ``task/briefing`` prompt.

    Args:
        run_id: The run ID to look up.

    Returns:
        ``{"ok": True, "context": {...}}`` when found, or
        ``{"ok": False, "error": "..."}`` when the run does not exist.
    """
    row = await get_run_context(run_id)
    if row is None:
        logger.warning("🔍 query_run_context: run_id=%r not found", run_id)
        return {"ok": False, "error": f"Run {run_id!r} not found"}
    logger.info("✅ query_run_context: found run_id=%r role=%r", run_id, row["role"])
    return {"ok": True, "context": dict(row)}


async def query_run_task(run_id: str) -> dict[str, object]:
    """Return the raw task_context string for a single run.

    Serves the ``ac://runs/{run_id}/task`` MCP resource.  Unlike
    ``query_run_context``, which returns the full structured context as JSON,
    this returns only the ``task_context`` field as plain text — the verbatim
    briefing that was injected into the agent's prompt at dispatch time.

    Args:
        run_id: The run ID to look up.

    Returns:
        ``{"ok": True, "task_context": "..."}`` when found, or
        ``{"ok": False, "error": "..."}`` when the run does not exist.
    """
    row = await get_run_context(run_id)
    if row is None:
        logger.warning("🔍 query_run_task: run_id=%r not found", run_id)
        return {"ok": False, "error": f"Run {run_id!r} not found"}
    task_context = row.get("task_context") or ""
    logger.info("✅ query_run_task: found run_id=%r (%d chars)", run_id, len(str(task_context)))
    return {"ok": True, "task_context": task_context}


async def query_children(run_id: str) -> dict[str, object]:
    """Return all runs spawned by *run_id*, ordered by spawn time.

    Coordinator and VP-tier agents use this to track the state of the
    engineers they dispatched.

    Args:
        run_id: The parent run ID.

    Returns:
        ``{"ok": True, "children": [...], "count": N}``
    """
    children = await get_children_by_parent_id(run_id)
    logger.info("✅ query_children: run_id=%r → %d child(ren)", run_id, len(children))
    return {"ok": True, "children": [dict(c) for c in children], "count": len(children)}


async def query_run_events(run_id: str, after_id: int = 0) -> dict[str, object]:
    """Return structured MCP events for *run_id* with ``id > after_id``.

    Agents can use this to reconstruct what happened in a previous session
    (i.e. after a crash and restart).  Pass ``after_id`` to page through
    events incrementally.

    Args:
        run_id: The run to query events for.
        after_id: Return only events with DB id strictly greater than this.

    Returns:
        ``{"ok": True, "events": [...], "count": N}``
    """
    events = await get_agent_events_tail(run_id, after_id=after_id)
    logger.info("✅ query_run_events: run_id=%r after_id=%d → %d event(s)", run_id, after_id, len(events))
    return {"ok": True, "events": [dict(e) for e in events], "count": len(events)}



async def query_active_runs() -> dict[str, object]:
    """Return all runs currently in a live or blocked state.

    Live statuses: ``pending_launch``, ``implementing``, ``reviewing``,
    ``blocked``.  Supervisory agents and the Dispatcher use this to get
    a snapshot of system-wide active work.

    Returns:
        ``{"ok": True, "runs": [...], "count": N}``
    """
    runs = await get_active_runs()
    logger.info("✅ query_active_runs: %d active run(s)", len(runs))
    return {"ok": True, "runs": [dict(r) for r in runs], "count": len(runs)}


async def query_run_tree(batch_id: str) -> dict[str, object]:
    """Return the full run tree for *batch_id* as a flat list.

    Each node contains ``id``, ``parent_run_id``, ``role``, ``status``,
    ``tier``, ``org_domain``, ``issue_number``, and ``spawned_at``.
    Assemble into a tree by following ``parent_run_id`` references.

    Args:
        batch_id: The batch fingerprint to query.

    Returns:
        ``{"ok": True, "nodes": [...], "count": N}``
    """
    nodes = await get_run_tree_by_batch_id(batch_id)
    logger.info("✅ query_run_tree: batch_id=%r → %d node(s)", batch_id, len(nodes))
    return {"ok": True, "nodes": [dict(n) for n in nodes], "count": len(nodes)}


async def query_dispatcher_state() -> dict[str, object]:
    """Return current dispatcher state for supervisory agents.

    Provides:
      - ``status_counts``  — run count per status across all time
      - ``active_count``   — total of pending_launch + implementing + reviewing + blocked
      - ``latest_batch_id``— batch_id of the most recently active wave (or null)

    Returns:
        ``{"ok": True, "status_counts": [...], "active_count": N, "latest_batch_id": "..." | null}``
    """
    counts = await get_run_status_counts()
    active_statuses = {"pending_launch", "implementing", "reviewing", "blocked"}
    active_count = sum(r["count"] for r in counts if r["status"] in active_statuses)
    latest_batch_id = await get_latest_active_batch_id()
    logger.info(
        "✅ query_dispatcher_state: active_count=%d latest_batch=%r",
        active_count, latest_batch_id,
    )
    return {
        "ok": True,
        "status_counts": [dict(c) for c in counts],
        "active_count": active_count,
        "latest_batch_id": latest_batch_id,
    }


async def query_run_status(run_id: str) -> dict[str, object]:
    """Return the current status of a run — coordinators use this to poll children.

    Designed for coordinator agents that need to know when their dispatched
    child runs have completed, failed, or are still running.

    The response includes the status string and, when the run has terminated,
    the ``completed_at`` timestamp.  Poll at a reasonable interval (every
    30–60 seconds) and stop when ``status`` is one of the terminal values.

    Terminal statuses: ``"completed"``, ``"cancelled"``, ``"stopped"``.
    Active statuses: ``"implementing"``, ``"reviewing"``, ``"pending_launch"``.

    Args:
        run_id: The run ID returned by ``build_spawn_adhoc_child``.

    Returns:
        ``{"ok": True, "run_id": str, "status": str, "completed_at": str|None}``
        ``{"ok": False, "error": str}`` when the run_id is not found.
    """
    row = await get_run_context(run_id)
    if row is None:
        logger.warning("⚠️ query_run_status: run_id %r not found", run_id)
        return {"ok": False, "error": f"run_id {run_id!r} not found"}
    logger.info("✅ query_run_status: run_id=%s status=%s", run_id, row["status"])
    return {
        "ok": True,
        "run_id": run_id,
        "status": row["status"],
        "completed_at": row.get("completed_at"),
    }


async def query_system_health() -> dict[str, object]:
    """Return a system-health snapshot for supervisory agents.

    Checks DB reachability and returns aggregate run counts per status.
    Always returns a result — ``db_ok: False`` signals a degraded database
    without raising an exception.

    Returns:
        ``{"ok": True, "db_ok": bool, "status_counts": [...], "total_runs": N}``
    """
    db_ok = await check_db_reachable()
    counts: list[dict[str, object]] = []
    total = 0
    if db_ok:
        status_counts = await get_run_status_counts()
        counts = [dict(c) for c in status_counts]
        total = sum(c["count"] for c in status_counts)
    logger.info("✅ query_system_health: db_ok=%r total_runs=%d", db_ok, total)
    return {"ok": True, "db_ok": db_ok, "status_counts": counts, "total_runs": total}
