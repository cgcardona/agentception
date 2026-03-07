from __future__ import annotations

"""Canonical agent status definitions — single source of truth.

Every module that needs to reason about agent lifecycle status imports from
here.  No other file may define its own ``_ACTIVE_STATUSES`` or equivalent.

Run lifecycle state machine
---------------------------
::

    pending_launch → implementing : build_claim_run
    pending_launch → cancelled    : build_cancel_run
    implementing   → blocked      : build_block_run
    implementing   → completed    : build_complete_run
    implementing   → cancelled    : build_cancel_run
    implementing   → stopped      : build_stop_run
    blocked        → implementing : build_resume_run
    stopped        → implementing : build_resume_run
    completed/cancelled/stopped/failed → terminal

``stale`` is not stored in the DB.  It is computed on-demand from
``last_activity_at`` and the :data:`STALE_THRESHOLD` constant.
"""

import datetime
import enum

STALE_THRESHOLD = datetime.timedelta(seconds=1800)
"""Runs with no activity for 30 minutes are considered stale (computed, not stored)."""


class AgentStatus(str, enum.Enum):
    """Canonical lifecycle states for an agent run.

    Values are lowercase strings matching the DB ``agent_runs.status`` column.
    """

    PENDING_LAUNCH = "pending_launch"
    IMPLEMENTING = "implementing"
    BLOCKED = "blocked"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    FAILED = "failed"


#: States indicating a run has (or recently had) a live worktree.
#: Used by the orphan sweep in ``persist.py``.  ``pending_launch`` is excluded
#: because pending runs exist only in the DB queue — including them would
#: immediately orphan them before the Dispatcher claims them.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.BLOCKED.value,
    AgentStatus.REVIEWING.value,
})

#: States considered "live" for UI hierarchy and staleness checks.
LIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.BLOCKED.value,
    AgentStatus.REVIEWING.value,
})

#: States reset to ``failed`` during a full build reset.
RESET_STATUSES: frozenset[str] = frozenset({
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.BLOCKED.value,
    AgentStatus.REVIEWING.value,
})

#: States that place an issue card in the ``active`` swim lane (when no PR exists).
LANE_ACTIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.BLOCKED.value,
    AgentStatus.REVIEWING.value,
})

#: Terminal states — no further transitions are possible.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    AgentStatus.COMPLETED.value,
    AgentStatus.CANCELLED.value,
    AgentStatus.STOPPED.value,
    AgentStatus.FAILED.value,
})

#: States that a run may be resumed from (blocked or stopped → implementing).
RESUMABLE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.BLOCKED.value,
    AgentStatus.STOPPED.value,
})


def is_active(status: str) -> bool:
    """Return ``True`` if *status* represents a run with a live worktree."""
    return status in ACTIVE_STATUSES


def is_live(status: str) -> bool:
    """Return ``True`` if *status* should appear as live in the UI."""
    return status in LIVE_STATUSES


def is_terminal(status: str) -> bool:
    """Return ``True`` if *status* is a terminal state (no further transitions)."""
    return status in TERMINAL_STATUSES


def compute_agent_status(
    raw_status: str,
    last_activity_at: datetime.datetime | None,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Normalise a raw status string and apply staleness logic.

    ``stale`` is computed from ``last_activity_at`` and is never stored in the
    DB.  It is returned here as a display-only value for the UI and queries
    layer.  Returns a status string suitable for display and lane computation.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    if raw_status in LIVE_STATUSES and last_activity_at is not None:
        if (now - last_activity_at) > STALE_THRESHOLD:
            return "stale"

    if raw_status in {s.value for s in AgentStatus}:
        return raw_status

    return AgentStatus.FAILED.value
