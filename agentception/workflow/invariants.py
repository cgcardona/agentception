from __future__ import annotations

"""Tick-level invariant checks and alerts for the workflow state machine.

Run after each tick to detect data inconsistencies and surface warnings
to maintainers.  Invariant violations are returned as alert strings (not
exceptions) so the pipeline never crashes due to a check.
"""

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)


class InvariantContext(TypedDict):
    """Data snapshot passed to the invariant checker."""

    repo: str
    issue_numbers: list[int]
    pr_numbers_in_db: set[int]
    run_pr_numbers: dict[str, int | None]
    link_issue_numbers_by_pr: dict[int, list[int]]
    workflow_states: dict[int, WorkflowSnapshot]
    pr_states: dict[int, str]
    pr_bases: dict[int, str | None]
    closes_refs_by_pr: dict[int, list[int]]


class WorkflowSnapshot(TypedDict):
    """Minimal workflow state fields for invariant checking."""

    lane: str
    pr_number: int | None
    pr_state: str | None
    agent_status: str | None
    issue_state: str


def check_invariants(ctx: InvariantContext) -> list[str]:
    """Run all invariant checks and return a list of alert strings.

    Each alert is a human-readable string prefixed with the invariant ID
    (e.g. ``INV-PR-LINK-1: ...``).  Returns an empty list when all checks pass.
    """
    alerts: list[str] = []

    alerts.extend(_inv_pr_link_1(ctx))
    alerts.extend(_inv_run_pr_1(ctx))
    alerts.extend(_inv_lane_1(ctx))
    alerts.extend(_inv_base_1(ctx))

    if alerts:
        logger.warning(
            "⚠️  %d invariant violation(s) detected: %s",
            len(alerts),
            "; ".join(alerts[:5]),
        )

    return alerts


def _inv_pr_link_1(ctx: InvariantContext) -> list[str]:
    """INV-PR-LINK-1: Every PR with ``Closes #N`` must have a link row."""
    alerts: list[str] = []
    for pr_num, close_refs in ctx["closes_refs_by_pr"].items():
        linked = ctx["link_issue_numbers_by_pr"].get(pr_num, [])
        for issue_num in close_refs:
            if issue_num not in linked:
                alerts.append(
                    f"INV-PR-LINK-1: PR #{pr_num} mentions Closes #{issue_num} "
                    f"but no link row exists"
                )
    return alerts


def _inv_run_pr_1(ctx: InvariantContext) -> list[str]:
    """INV-RUN-PR-1: Runs with pr_number must have a matching PR row or warning."""
    alerts: list[str] = []
    for run_id, pr_num in ctx["run_pr_numbers"].items():
        if pr_num is not None and pr_num not in ctx["pr_numbers_in_db"]:
            alerts.append(
                f"INV-RUN-PR-1: Run {run_id} has pr_number={pr_num} "
                f"but PR is not in ac_pull_requests"
            )
    return alerts


def _inv_lane_1(ctx: InvariantContext) -> list[str]:
    """INV-LANE-1: pr_open/reviewing lanes must have an open best-PR."""
    alerts: list[str] = []
    for issue_num, ws in ctx["workflow_states"].items():
        if ws["lane"] in ("pr_open", "reviewing"):
            if ws["pr_number"] is None:
                alerts.append(
                    f"INV-LANE-1: Issue #{issue_num} in lane '{ws['lane']}' "
                    f"but has no linked PR"
                )
            elif ws["pr_state"] not in ("open", "draft"):
                alerts.append(
                    f"INV-LANE-1: Issue #{issue_num} in lane '{ws['lane']}' "
                    f"but PR #{ws['pr_number']} state is '{ws['pr_state']}'"
                )
    return alerts


def _inv_base_1(ctx: InvariantContext) -> list[str]:
    """INV-BASE-1: PRs targeting non-dev base branches."""
    alerts: list[str] = []
    for pr_num, base in ctx["pr_bases"].items():
        if base is not None and base != "dev" and ctx["pr_states"].get(pr_num) == "open":
            alerts.append(
                f"INV-BASE-1: PR #{pr_num} targets '{base}' instead of 'dev'"
            )
    return alerts
