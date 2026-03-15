from __future__ import annotations

"""Phase gate computation for the pipeline control panel.

Computes gate status (waiting / ready / done) for each phase label in order,
based on the open issues visible in the board and the active agents list.

This is a **pure function module** — no I/O, no side effects.  The route
handler calls :func:`compute_phase_lanes` and injects the result into the
Jinja context.  Keeping gate logic here lets tests verify it without touching
HTTP or template rendering.

Gate semantics
--------------
Given ``labels = [L0, L1, L2, ...]``:

- ``done``    — phase has **0** open issues (all closed or never started).
- ``waiting`` — phase has open issues AND at least one *upstream* phase (earlier
  in the order) also has open issues.  The first upstream blocker is exposed
  as ``blockers``.
- ``ready``   — phase has open issues AND **all** upstream phases are done.
  Work can begin here.
"""

import logging

from agentception.models import AgentNode, BoardIssue
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

# Palette cycled by phase index so each lane gets a distinct dot colour.
_PHASE_COLORS: list[str] = [
    "#ef4444",  # red
    "#f97316",  # orange
    "#eab308",  # yellow
    "#22c55e",  # green
    "#3b82f6",  # blue
    "#8b5cf6",  # violet
    "#ec4899",  # pink
    "#14b8a6",  # teal
]

GateStatus = str  # "waiting" | "ready" | "done"

BlockerItem = dict[str, JsonValue]   # {number: int, title: str}
PhaseLane = dict[str, JsonValue]     # see compute_phase_lanes docstring


def compute_phase_lanes(
    labels: list[str],
    board_issues: list[BoardIssue],
    agents: list[AgentNode],
) -> list[PhaseLane]:
    """Return one lane dict per label, enriched with gate status and blockers.

    Each returned dict has the shape::

        {
          "label":       str,
          "color":       str,   # hex colour for the dot
          "open_count":  int,   # open issues in this phase
          "total_count": int,   # same as open_count (no closed-per-phase data)
          "agent_count": int,   # agents whose issue is in this phase
          "gate_status": str,   # "waiting" | "ready" | "done"
          "blockers":    list[{"number": int, "title": str}],
        }

    Parameters
    ----------
    labels:
        Ordered phase labels from ``active_labels_order`` in the pipeline
        config.  Earlier = upstream.
    board_issues:
        Open issues visible in the board, each carrying a ``phase_label``.
    agents:
        Active agent nodes, each carrying an ``issue_number``.

    Notes
    -----
    - Issues whose ``phase_label`` is absent from *labels* are silently
      ignored; they belong to phases outside the configured order.
    - ``total_count`` equals ``open_count`` because the board only carries
      open issues.  A future enhancement could query closed counts from
      Postgres and pass them in.
    """
    if not labels:
        return []

    # Build label → open issues map.
    open_by_label: dict[str, list[BoardIssue]] = {lbl: [] for lbl in labels}
    for issue in board_issues:
        if issue.phase_label and issue.phase_label in open_by_label:
            open_by_label[issue.phase_label].append(issue)

    # Map issue_number → phase_label for agent counting.
    issue_to_label: dict[int, str] = {}
    for issue in board_issues:
        if issue.phase_label and issue.phase_label in open_by_label:
            issue_to_label[issue.number] = issue.phase_label

    # Count agents per phase.
    agent_count_by_label: dict[str, int] = {lbl: 0 for lbl in labels}
    for agent in agents:
        if agent.issue_number is not None:
            lbl = issue_to_label.get(agent.issue_number)
            if lbl is not None:
                agent_count_by_label[lbl] = agent_count_by_label.get(lbl, 0) + 1

    lanes: list[PhaseLane] = []
    for idx, label in enumerate(labels):
        open_issues = open_by_label[label]
        open_count = len(open_issues)
        upstream_labels = labels[:idx]

        gate_status: GateStatus
        blockers: list[JsonValue]

        if open_count == 0:
            gate_status = "done"
            blockers = []
        else:
            blocking_phase_issues: list[BoardIssue] = []
            for up_label in upstream_labels:
                if open_by_label[up_label]:
                    blocking_phase_issues = open_by_label[up_label]
                    break

            if blocking_phase_issues:
                gate_status = "waiting"
                blockers = [
                    {"number": i.number, "title": i.title}
                    for i in blocking_phase_issues
                ]
            else:
                gate_status = "ready"
                blockers = []

        lanes.append(
            {
                "label": label,
                "color": _PHASE_COLORS[idx % len(_PHASE_COLORS)],
                "open_count": open_count,
                "total_count": open_count,
                "agent_count": agent_count_by_label[label],
                "gate_status": gate_status,
                "blockers": blockers,
            }
        )

    logger.debug("✅ Computed %d phase lanes", len(lanes))
    return lanes
