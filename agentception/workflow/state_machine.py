from __future__ import annotations

"""Canonical workflow state machine — deterministic swim-lane computation.

This module is the **single place** that decides which swim lane an issue
card belongs in.  No other code (Jinja2, JS, queries, build_ui) may
recompute lanes.  The UI reads persisted ``ACIssueWorkflowState.lane``.

State machine inputs (all from DB):
- Issue row (state, labels, phase)
- Best-linked PR (from ``workflow.linking``)
- Latest agent run
- Merge/close history

Output:
- A ``WorkflowState`` dataclass persisted to ``ac_issue_workflow_state``.

Lane rules (explicit state machine — no "if ladder"):

    DONE       — issue closed  OR  (PR merged AND closure stable)
    REVIEWING  — PR open AND agent_status == reviewing
    PR_OPEN    — PR open (any link method, any base branch)
    ACTIVE     — agent run in an active status, no open PR
    TODO       — none of the above
"""

import hashlib
import json
import logging
from typing import TypedDict

from agentception.workflow.linking import BestPR
from agentception.workflow.status import LANE_ACTIVE_STATUSES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------------------


class IssueInput(TypedDict):
    """Minimal issue fields consumed by the state machine."""

    number: int
    state: str
    labels: list[str]
    phase_key: str | None
    initiative: str | None


class RunInput(TypedDict):
    """Minimal agent-run fields consumed by the state machine."""

    id: str
    status: str
    agent_status: str
    pr_number: int | None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class WorkflowState(TypedDict):
    """Canonical computed workflow state for one issue."""

    lane: str
    issue_state: str
    run_id: str | None
    agent_status: str | None
    pr_number: int | None
    pr_state: str | None
    pr_base: str | None
    pr_head_ref: str | None
    pr_link_method: str | None
    pr_link_confidence: int | None
    warnings: list[str]
    content_hash: str


# ---------------------------------------------------------------------------
# Lane values
# ---------------------------------------------------------------------------

LANE_TODO = "todo"
LANE_ACTIVE = "active"
LANE_PR_OPEN = "pr_open"
LANE_REVIEWING = "reviewing"
LANE_DONE = "done"

VALID_LANES: frozenset[str] = frozenset({
    LANE_TODO, LANE_ACTIVE, LANE_PR_OPEN, LANE_REVIEWING, LANE_DONE,
})


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_workflow_state(
    issue: IssueInput,
    run: RunInput | None,
    best_pr: BestPR | None,
    *,
    pr_merged_recently: bool = False,
) -> WorkflowState:
    """Compute the canonical workflow state for a single issue.

    Parameters
    ----------
    issue:
        Issue data from ``ac_issues``.
    run:
        Most recent agent run for this issue (from ``ac_agent_runs``), or ``None``.
    best_pr:
        Best-linked PR (from ``workflow.linking.best_pr_for_issue``), or ``None``.
    pr_merged_recently:
        If ``True`` and the issue is still open, stabilise the lane as ``done``
        to prevent flicker during GitHub's auto-close propagation window.

    Returns
    -------
    WorkflowState
        Deterministic state with lane, PR info, warnings, and content hash.
    """
    warnings: list[str] = []

    # --- Gather PR fields ---
    pr_number = best_pr["pr_number"] if best_pr else None
    pr_state = best_pr["pr_state"] if best_pr else None
    pr_base = best_pr["pr_base"] if best_pr else None
    pr_head_ref = best_pr["pr_head_ref"] if best_pr else None
    pr_link_method = best_pr["link_method"] if best_pr else None
    pr_link_confidence = best_pr["confidence"] if best_pr else None

    # --- Gather run fields ---
    run_id = run["id"] if run else None
    agent_status = run["agent_status"] if run else None

    # --- Check for run→PR mismatch ---
    if run and run["pr_number"] is not None and best_pr is None:
        warnings.append(
            f"run_claims_pr_missing_from_links: run {run['id']} "
            f"has pr_number={run['pr_number']} but no PR link found"
        )

    # --- Base branch mismatch warning ---
    if pr_base is not None and pr_base != "dev":
        warnings.append(
            f"wrong_base: PR #{pr_number} targets '{pr_base}' instead of 'dev'"
        )

    # --- Compute lane ---
    lane = _compute_lane(
        issue_state=issue["state"],
        agent_status=agent_status,
        pr_state=pr_state,
        pr_merged_recently=pr_merged_recently,
    )

    # --- Issue state for persistence ---
    issue_state = issue["state"]
    if pr_merged_recently and issue_state == "open":
        issue_state = "close_pending"
        if lane != LANE_DONE:
            lane = LANE_DONE
        warnings.append("issue_close_pending_propagation")

    # --- Content hash for update guard ---
    content_hash = _state_hash(
        lane, issue_state, run_id, agent_status,
        pr_number, pr_state, pr_base, pr_head_ref,
        pr_link_method, pr_link_confidence, warnings,
    )

    return WorkflowState(
        lane=lane,
        issue_state=issue_state,
        run_id=run_id,
        agent_status=agent_status,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_base=pr_base,
        pr_head_ref=pr_head_ref,
        pr_link_method=pr_link_method,
        pr_link_confidence=pr_link_confidence,
        warnings=warnings,
        content_hash=content_hash,
    )


def _compute_lane(
    *,
    issue_state: str,
    agent_status: str | None,
    pr_state: str | None,
    pr_merged_recently: bool,
) -> str:
    """Pure lane computation from normalised signals.

    Priority (highest wins):
    1. DONE      — issue closed or PR merged (with stabilisation)
    2. REVIEWING — open PR and agent reviewing
    3. PR_OPEN   — open PR
    4. ACTIVE    — active agent, no open PR
    5. TODO      — fallback
    """
    if issue_state == "closed":
        return LANE_DONE

    if pr_state == "merged":
        return LANE_DONE

    if pr_merged_recently:
        return LANE_DONE

    if pr_state in ("open", "draft"):
        if agent_status == "reviewing":
            return LANE_REVIEWING
        return LANE_PR_OPEN

    if agent_status is not None and agent_status in LANE_ACTIVE_STATUSES:
        return LANE_ACTIVE

    return LANE_TODO


def _state_hash(
    lane: str,
    issue_state: str,
    run_id: str | None,
    agent_status: str | None,
    pr_number: int | None,
    pr_state: str | None,
    pr_base: str | None,
    pr_head_ref: str | None,
    pr_link_method: str | None,
    pr_link_confidence: int | None,
    warnings: list[str],
) -> str:
    """Produce a deterministic hash of all state fields for the update guard."""
    blob = json.dumps(
        [
            lane, issue_state, run_id, agent_status,
            pr_number, pr_state, pr_base, pr_head_ref,
            pr_link_method, pr_link_confidence, sorted(warnings),
        ],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()
