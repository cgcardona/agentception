from __future__ import annotations

"""Canonical agent status definitions — single source of truth.

Every module that needs to reason about agent lifecycle status imports from
here.  No other file may define its own ``_ACTIVE_STATUSES`` or equivalent.
"""

import datetime
import enum

STALE_THRESHOLD = datetime.timedelta(seconds=1800)
"""Runs with no activity for 30 minutes are considered stale."""


class AgentStatus(str, enum.Enum):
    """Canonical lifecycle states for an agent run.

    Values are lowercase strings matching the DB ``agent_runs.status`` column.
    """

    PENDING_LAUNCH = "pending_launch"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    DONE = "done"
    STALE = "stale"
    UNKNOWN = "unknown"


ACTIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.REVIEWING.value,
    AgentStatus.STALE.value,
})
"""Statuses indicating a run has (or recently had) a live worktree.

Used by the orphan sweep in ``persist.py``.  ``pending_launch`` is excluded
because pending runs exist only in the DB queue — including them would
immediately orphan them before the Dispatcher claims them.
"""

LIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.REVIEWING.value,
})
"""Statuses considered "live" for UI hierarchy and staleness checks."""

RESET_STATUSES: frozenset[str] = frozenset({
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.REVIEWING.value,
})
"""Statuses reset to ``unknown`` during a full build reset."""

LANE_ACTIVE_STATUSES: frozenset[str] = frozenset({
    AgentStatus.IMPLEMENTING.value,
    AgentStatus.PENDING_LAUNCH.value,
    AgentStatus.STALE.value,
    AgentStatus.REVIEWING.value,
})
"""Statuses that place an issue card in the ``active`` swim lane (when no PR exists)."""


def is_active(status: str) -> bool:
    """Return ``True`` if *status* represents a run with a live worktree."""
    return status in ACTIVE_STATUSES


def is_live(status: str) -> bool:
    """Return ``True`` if *status* should appear as live in the UI."""
    return status in LIVE_STATUSES


def compute_agent_status(
    raw_status: str,
    last_activity_at: datetime.datetime | None,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Normalise a raw status string and apply staleness logic.

    Returns a status string suitable for display and lane computation.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    if raw_status in LIVE_STATUSES and last_activity_at is not None:
        if (now - last_activity_at) > STALE_THRESHOLD:
            return AgentStatus.STALE.value

    if raw_status in {s.value for s in AgentStatus}:
        return raw_status

    return AgentStatus.UNKNOWN.value
