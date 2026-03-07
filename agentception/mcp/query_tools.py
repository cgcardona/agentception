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

from agentception.db.queries import get_pending_launches

logger = logging.getLogger(__name__)


async def query_pending_runs() -> dict[str, object]:
    """Return all pending launch records from the AgentCeption DB.

    The Dispatcher calls this once to discover what the UI has queued.
    Each item in ``pending`` contains:
      - ``run_id``             — worktree id (e.g. "issue-1234")
      - ``issue_number``       — GitHub issue number
      - ``role``               — role slug (e.g. "cto", "python-developer")
      - ``branch``             — git branch to work on
      - ``host_worktree_path`` — full path on the HOST filesystem
      - ``batch_id``           — batch fingerprint

    Was: ``build_get_pending_launches``.

    Returns:
        ``{"pending": [...], "count": N}``
    """
    logger.warning("🔍 query_pending_runs: querying DB for pending launches")
    launches = await get_pending_launches()
    logger.warning(
        "🔍 query_pending_runs: got %d row(s) from DB",
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
