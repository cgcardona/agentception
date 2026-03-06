from __future__ import annotations

"""Read-only query helpers for AgentCeption's Postgres data store.

All functions return plain dicts / lists so callers (routes, poller) have
zero dependency on SQLAlchemy internals.  Swallows DB errors and returns
empty results so a database outage degrades gracefully to in-memory state.
"""

import datetime
import fnmatch
import json
import logging
import re as _re
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select, text

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentEvent,
    ACAgentMessage,
    ACAgentRun,
    ACIssue,
    ACIssueWorkflowState,
    ACPipelineSnapshot,
    ACPullRequest,
    ACWave,
)


# ---------------------------------------------------------------------------
# Row TypedDicts — typed return shapes for every query function.
# All fields match the dict literals built in each query body exactly.
# ---------------------------------------------------------------------------


class LabelEntry(TypedDict):
    """Single label object as returned by the GitHub API shape."""

    name: str


class BoardIssueRow(TypedDict):
    """One row from get_board_issues."""

    number: int
    title: str
    state: str
    labels: list[LabelEntry]
    claimed: bool
    phase_label: str | None
    last_synced_at: str


class PipelineTrendRow(TypedDict):
    """One snapshot row from get_pipeline_trend."""

    polled_at: str
    active_label: str | None
    issues_open: int
    prs_open: int
    agents_active: int
    alert_count: int


class AgentRunRow(TypedDict):
    """One row from get_agent_run_history."""

    id: str
    wave_id: str | None
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    worktree_path: str | None
    role: str
    status: str
    attempt_number: int
    spawn_mode: str | None
    batch_id: str | None
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None


class AgentMessageRow(TypedDict):
    """One transcript message row from get_agent_run_detail."""

    role: str
    content: str | None
    tool_name: str | None
    sequence_index: int
    recorded_at: str


class AgentRunDetail(TypedDict):
    """Full detail dict from get_agent_run_detail."""

    id: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    role: str
    status: str
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None
    batch_id: str | None
    cognitive_arch: str | None
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    messages: list[AgentMessageRow]


class SiblingRunRow(TypedDict):
    """Minimal sibling agent run info for the lineage panel."""

    id: str
    role: str
    status: str
    issue_number: int | None
    tier: str | None


class AgentRunTeardownRow(TypedDict):
    """Minimal agent run fields needed to tear down a worktree after completion."""

    worktree_path: str | None
    branch: str | None


class OpenPRRow(TypedDict):
    """One row from get_open_prs_db."""

    number: int
    title: str
    state: str
    headRefName: str | None
    labels: list[LabelEntry]


class LinkedPRRow(TypedDict):
    """Linked PR summary embedded in IssueDetailRow."""

    number: int
    title: str
    state: str
    head_ref: str | None
    merged_at: str | None


class IssueAgentRunRow(TypedDict):
    """Agent run summary embedded in IssueDetailRow."""

    id: str
    role: str
    status: str
    branch: str | None
    pr_number: int | None
    spawned_at: str
    last_activity_at: str | None


class IssueDetailRow(TypedDict):
    """Full detail dict from get_issue_detail."""

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    phase_label: str | None
    claimed: bool
    first_seen_at: str
    last_synced_at: str
    closed_at: str | None
    linked_prs: list[LinkedPRRow]
    agent_runs: list[IssueAgentRunRow]


class AllIssueRow(TypedDict):
    """One row from get_all_issues."""

    number: int
    title: str
    state: str
    labels: list[str]
    phase_label: str | None
    closed_at: str | None
    last_synced_at: str


class LinkedIssueRow(TypedDict):
    """Linked issue summary embedded in PRDetailRow."""

    number: int
    title: str
    state: str


class PRAgentRunRow(TypedDict):
    """Agent run summary embedded in PRDetailRow."""

    id: str
    role: str
    status: str
    branch: str | None
    issue_number: int | None
    spawned_at: str
    last_activity_at: str | None


class PRDetailRow(TypedDict):
    """Full detail dict from get_pr_detail."""

    number: int
    title: str
    state: str
    head_ref: str | None
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    first_seen_at: str
    last_synced_at: str
    linked_issue: LinkedIssueRow | None
    agent_runs: list[PRAgentRunRow]


class AllPRRow(TypedDict):
    """One row from get_all_prs."""

    number: int
    title: str
    state: str
    head_ref: str | None
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    last_synced_at: str


class ShipReviewerRunRow(TypedDict):
    """Latest pr-reviewer run attached to a PR on the Ship board."""

    id: str
    status: str
    spawned_at: str
    last_activity_at: str | None


class ShipPRRow(TypedDict):
    """Enriched PR entry for the Ship board."""

    number: int
    title: str
    state: str
    head_ref: str | None
    url: str
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    phase_label: str | None
    reviewer_run: ShipReviewerRunRow | None


class ShipPhaseGroupRow(TypedDict):
    """PRs grouped by phase label for the Ship board."""

    label: str
    prs: list[ShipPRRow]


class WaveAgentRow(TypedDict):
    """One agent entry inside a WaveRow."""

    id: str
    role: str
    status: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    batch_id: str | None
    worktree_path: str | None
    cognitive_arch: str | None
    message_count: int


class WaveRow(TypedDict):
    """One wave from get_waves_from_db."""

    batch_id: str
    started_at: float
    ended_at: float | None
    issues_worked: list[int]
    prs_opened: int
    prs_merged: int
    estimated_tokens: int
    estimated_cost_usd: float
    agents: list[WaveAgentRow]


class ConductorHistoryRow(TypedDict):
    """One entry from get_conductor_history."""

    wave_id: str
    worktree: str
    host_worktree: str
    started_at: str
    status: str


class PhasedIssueRow(TypedDict):
    """One issue entry inside a PhaseGroupRow."""

    number: int
    title: str
    body_excerpt: str
    """First ~120 chars of the issue body, markdown stripped — used as a card subtitle."""
    state: str
    url: str
    labels: list[str]
    depends_on: list[int]
    """GitHub issue numbers this issue must wait for (ticket-level dependencies)."""


class PhaseGroupRow(TypedDict):
    """One phase bucket from get_issues_grouped_by_phase."""

    label: str
    issues: list[PhasedIssueRow]
    locked: bool
    complete: bool
    depends_on: list[str]


class OpenPRForIssueRow(TypedDict):
    """An open GitHub PR associated with a board issue.

    Returned by ``get_open_prs_by_issue`` which uses this as the authoritative
    signal for placing issues in the ``pr_open`` or ``reviewing`` swim lane.
    Two matching strategies are used (either is sufficient):
    1. ``closes_issue_number`` — explicit ``Closes #N`` link in the PR body.
    2. ``head_ref`` matching ``feat/issue-{N}-*`` — branch naming convention.
    """

    pr_number: int
    head_ref: str | None


class WorkflowStateRow(TypedDict):
    """Canonical workflow state for a board issue, read from ``ac_issue_workflow_state``.

    This is the UI's source of truth for swim lanes — no ad-hoc inference.
    """

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


class RunForIssueRow(TypedDict):
    """Most-recent run entry from get_runs_for_issue_numbers.

    ``agent_status`` is a normalized, stale-aware status string suitable for
    CSS class suffixes: ``implementing`` | ``reviewing`` | ``done`` | ``stale``
    | ``unknown`` | other DB values lower-cased.  A run is ``stale`` when its
    status is active but ``last_activity_at`` is older than
    ``_STALE_THRESHOLD_SECONDS``.
    """

    id: str
    role: str
    cognitive_arch: str | None
    status: str
    agent_status: str
    pr_number: int | None
    branch: str | None
    spawned_at: str
    last_activity_at: str | None
    current_step: str | None
    steps_completed: int
    tier: str | None
    org_domain: str | None
    batch_id: str | None


class RunTreeNodeRow(TypedDict):
    """One node in the agent run tree, returned by ``get_run_tree_by_batch_id``.

    The flat list can be assembled into a tree client-side by following
    ``parent_run_id`` references.
    """

    id: str
    role: str
    status: str
    agent_status: str
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    issue_number: int | None
    pr_number: int | None
    batch_id: str | None
    spawned_at: str
    last_activity_at: str | None
    current_step: str | None


# ---------------------------------------------------------------------------
# Step-data helpers — used internally by get_runs_for_issue_numbers
# ---------------------------------------------------------------------------

from agentception.workflow.status import (
    LIVE_STATUSES as _LIVE_STATUSES,
    STALE_THRESHOLD,
)

#: Statuses considered "active" for staleness detection (same as live for queries).
_ACTIVE_STATUSES = _LIVE_STATUSES

#: Seconds threshold — derived from the canonical timedelta.
_STALE_THRESHOLD_SECONDS: int = int(STALE_THRESHOLD.total_seconds())

#: Branch naming convention for engineer feature branches.
#: Group 1 captures the issue number so we can link the PR back to its issue.
_AC_ISSUE_BRANCH_RE: _re.Pattern[str] = _re.compile(r"^ac/issue-(\d+)")


class _RunStepData(TypedDict):
    """Internal shape returned by ``_get_step_data_for_runs``."""

    current_step: str | None
    steps_completed: int


class PendingLaunchRow(TypedDict):
    """One pending launch from get_pending_launches."""

    run_id: str
    issue_number: int | None
    role: str
    branch: str | None
    worktree_path: str | None
    host_worktree_path: str | None
    batch_id: str | None
    spawned_at: str
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None


class AgentEventRow(TypedDict):
    """One structured event from get_agent_events_tail.

    ``payload`` is the raw JSON string stored in the DB — callers must
    parse it with ``json.loads`` if they need the structured payload.
    """

    id: int
    event_type: str
    payload: str
    recorded_at: str


class AgentThoughtRow(TypedDict):
    """One transcript message from get_agent_thoughts_tail."""

    seq: int
    role: str
    content: str
    recorded_at: str

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Board issues — replaces live gh CLI call in the overview sidebar
# ---------------------------------------------------------------------------


async def get_board_issues(
    repo: str,
    label: str | None = None,
    include_claimed: bool = False,
    limit: int = 50,
) -> list[BoardIssueRow]:
    """Return open issues from ``issues``, optionally filtered by phase label.

    Returns dicts shaped like the ``gh`` CLI JSON output so existing templates
    work without changes.  Falls back to ``[]`` on any DB error.
    """
    try:
        async with get_session() as session:
            stmt = (
                select(ACIssue)
                .where(ACIssue.repo == repo, ACIssue.state == "open")
                .order_by(ACIssue.github_number.desc())
                .limit(limit)
            )
            if label:
                # Filter to issues whose labels_json contains the phase label.
                # Using a text fragment is simpler than a JSON operator for
                # cross-dialect compatibility (works on Postgres and SQLite).
                stmt = stmt.where(ACIssue.labels_json.contains(label))
            result = await session.execute(stmt)
            rows = result.scalars().all()

        issues: list[BoardIssueRow] = []
        for row in rows:
            labels = json.loads(row.labels_json)
            is_claimed = "agent:wip" in labels
            if not include_claimed and is_claimed:
                continue
            issues.append(
                BoardIssueRow(
                    number=row.github_number,
                    title=row.title,
                    state=row.state,
                    labels=[LabelEntry(name=n) for n in labels],
                    claimed=is_claimed,
                    phase_label=row.phase_label,
                    last_synced_at=row.last_synced_at.isoformat(),
                )
            )
        return issues
    except Exception as exc:
        logger.warning("⚠️  get_board_issues DB query failed (non-fatal): %s", exc)
        return []


async def get_board_counts(
    repo: str,
    label: str | None = None,
) -> dict[str, int]:
    """Return unclaimed/claimed/total counts for the active phase board."""
    try:
        async with get_session() as session:
            stmt = select(ACIssue).where(
                ACIssue.repo == repo, ACIssue.state == "open"
            )
            if label:
                stmt = stmt.where(ACIssue.labels_json.contains(label))
            result = await session.execute(stmt)
            rows = result.scalars().all()

        total = len(rows)
        claimed = sum(
            1 for r in rows if "agent:wip" in json.loads(r.labels_json)
        )
        return {"total": total, "claimed": claimed, "unclaimed": total - claimed}
    except Exception as exc:
        logger.warning("⚠️  get_board_counts DB query failed (non-fatal): %s", exc)
        return {"total": 0, "claimed": 0, "unclaimed": 0}


# ---------------------------------------------------------------------------
# Pipeline trend — replaces ephemeral in-memory data on the telemetry page
# ---------------------------------------------------------------------------


async def get_pipeline_trend(
    hours: int = 24,
    limit: int = 500,
) -> list[PipelineTrendRow]:
    """Return recent pipeline snapshots for trend charts.

    Each dict has: ``polled_at`` (ISO string), ``active_label``,
    ``issues_open``, ``prs_open``, ``agents_active``, ``alert_count``.
    """
    try:
        async with get_session() as session:
            stmt = (
                select(ACPipelineSnapshot)
                .order_by(ACPipelineSnapshot.polled_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            PipelineTrendRow(
                polled_at=row.polled_at.isoformat(),
                active_label=row.active_label,
                issues_open=row.issues_open,
                prs_open=row.prs_open,
                agents_active=row.agents_active,
                alert_count=len(json.loads(row.alerts_json)),
            )
            for row in reversed(rows)  # chronological order for charts
        ]
    except Exception as exc:
        logger.warning("⚠️  get_pipeline_trend DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Agent run history — enriches the agents list / detail pages
# ---------------------------------------------------------------------------


async def get_agent_run_history(
    limit: int = 100,
    status: str | None = None,
) -> list[AgentRunRow]:
    """Return recent agent runs from ``agent_runs``, newest first."""
    try:
        async with get_session() as session:
            stmt = (
                select(ACAgentRun)
                .order_by(ACAgentRun.spawned_at.desc())
                .limit(limit)
            )
            if status:
                stmt = stmt.where(ACAgentRun.status == status)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            AgentRunRow(
                id=row.id,
                wave_id=row.wave_id,
                issue_number=row.issue_number,
                pr_number=row.pr_number,
                branch=row.branch,
                worktree_path=row.worktree_path,
                role=row.role,
                status=row.status,
                attempt_number=row.attempt_number,
                spawn_mode=row.spawn_mode,
                batch_id=row.batch_id,
                spawned_at=row.spawned_at.isoformat(),
                last_activity_at=(
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
                completed_at=(
                    row.completed_at.isoformat() if row.completed_at else None
                ),
                tier=row.tier,
                org_domain=row.org_domain,
                parent_run_id=row.parent_run_id,
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_history DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_run_detail(
    run_id: str,
) -> AgentRunDetail | None:
    """Return a single agent run with its transcript messages.

    ``run_id`` is the worktree basename (e.g. ``issue-732``), which is the
    primary key stored in ``agent_runs``.
    """
    try:
        async with get_session() as session:
            run_result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                return None

            msg_result = await session.execute(
                select(ACAgentMessage)
                .where(ACAgentMessage.agent_run_id == run_id)
                .order_by(ACAgentMessage.sequence_index)
            )
            messages = msg_result.scalars().all()

        return AgentRunDetail(
            id=run.id,
            issue_number=run.issue_number,
            pr_number=run.pr_number,
            branch=run.branch,
            role=run.role,
            status=run.status,
            spawned_at=run.spawned_at.isoformat(),
            last_activity_at=(
                run.last_activity_at.isoformat() if run.last_activity_at else None
            ),
            completed_at=(
                run.completed_at.isoformat() if run.completed_at else None
            ),
            batch_id=run.batch_id,
            cognitive_arch=run.cognitive_arch,
            tier=run.tier,
            org_domain=run.org_domain,
            parent_run_id=run.parent_run_id,
            messages=[
                AgentMessageRow(
                    role=m.role,
                    content=m.content,
                    tool_name=m.tool_name,
                    sequence_index=m.sequence_index,
                    recorded_at=m.recorded_at.isoformat(),
                )
                for m in messages
            ],
        )
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_detail DB query failed (non-fatal): %s", exc)
        return None


async def get_sibling_runs(
    batch_id: str,
    exclude_id: str,
) -> list[SiblingRunRow]:
    """Return all agent runs in the same batch, excluding the current run.

    Used by the agent profile page to render the lineage panel.  Returns an
    empty list when the batch has no other members or on any DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(
                    ACAgentRun.batch_id == batch_id,
                    ACAgentRun.id != exclude_id,
                )
                .order_by(ACAgentRun.spawned_at)
            )
            rows = result.scalars().all()
        return [
            SiblingRunRow(
                id=r.id,
                role=r.role,
                status=r.status,
                issue_number=r.issue_number,
                tier=r.tier,
            )
            for r in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_sibling_runs DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Open PRs — replaces gh CLI PR reads
# ---------------------------------------------------------------------------


async def get_open_prs_db(repo: str, limit: int = 50) -> list[OpenPRRow]:
    """Return open PRs from ``pull_requests``."""
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACPullRequest)
                .where(ACPullRequest.repo == repo, ACPullRequest.state == "open")
                .order_by(ACPullRequest.github_number.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            OpenPRRow(
                number=row.github_number,
                title=row.title,
                state=row.state,
                headRefName=row.head_ref,
                labels=[LabelEntry(name=n) for n in json.loads(row.labels_json)],
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_open_prs_db DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Issue detail — single issue with linked PR and agent runs
# ---------------------------------------------------------------------------


async def get_issue_detail(
    repo: str,
    number: int,
) -> IssueDetailRow | None:
    """Return full detail for a single issue from ``issues``.

    Includes linked PR (via ``closes_issue_number``) and all agent runs
    that worked on this issue.  Returns ``None`` when the issue is not in DB.
    """
    try:
        async with get_session() as session:
            issue_result = await session.execute(
                select(ACIssue).where(
                    ACIssue.repo == repo,
                    ACIssue.github_number == number,
                )
            )
            issue = issue_result.scalar_one_or_none()
            if issue is None:
                return None

            pr_result = await session.execute(
                select(ACPullRequest).where(
                    ACPullRequest.repo == repo,
                    ACPullRequest.closes_issue_number == number,
                )
            )
            linked_prs = pr_result.scalars().all()

            runs_result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.issue_number == number)
                .order_by(ACAgentRun.spawned_at.desc())
                .limit(20)
            )
            runs = runs_result.scalars().all()

        labels = json.loads(issue.labels_json)
        return IssueDetailRow(
            number=issue.github_number,
            title=issue.title,
            body=issue.body or "",
            state=issue.state,
            labels=labels,
            phase_label=issue.phase_label,
            claimed="agent:wip" in labels,
            first_seen_at=issue.first_seen_at.isoformat(),
            last_synced_at=issue.last_synced_at.isoformat(),
            closed_at=issue.closed_at.isoformat() if issue.closed_at else None,
            linked_prs=[
                LinkedPRRow(
                    number=pr.github_number,
                    title=pr.title,
                    state=pr.state,
                    head_ref=pr.head_ref,
                    merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                )
                for pr in linked_prs
            ],
            agent_runs=[
                IssueAgentRunRow(
                    id=r.id,
                    role=r.role,
                    status=r.status,
                    branch=r.branch,
                    pr_number=r.pr_number,
                    spawned_at=r.spawned_at.isoformat(),
                    last_activity_at=r.last_activity_at.isoformat() if r.last_activity_at else None,
                )
                for r in runs
            ],
        )
    except Exception as exc:
        logger.warning("⚠️  get_issue_detail DB query failed (non-fatal): %s", exc)
        return None


async def get_all_issues(
    repo: str,
    state: str | None = None,
    limit: int = 200,
) -> list[AllIssueRow]:
    """Return issues from ``issues``, optionally filtered by state."""
    try:
        async with get_session() as session:
            stmt = (
                select(ACIssue)
                .where(ACIssue.repo == repo)
                .order_by(ACIssue.github_number.desc())
                .limit(limit)
            )
            if state:
                stmt = stmt.where(ACIssue.state == state)
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [
            AllIssueRow(
                number=row.github_number,
                title=row.title,
                state=row.state,
                labels=json.loads(row.labels_json),
                phase_label=row.phase_label,
                closed_at=row.closed_at.isoformat() if row.closed_at else None,
                last_synced_at=row.last_synced_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_all_issues DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# PR detail — single PR with CI checks and agent runs
# ---------------------------------------------------------------------------


async def get_pr_detail(
    repo: str,
    number: int,
) -> PRDetailRow | None:
    """Return full detail for a single PR from ``pull_requests``.

    Includes linked issue and agent runs that worked on this PR.
    Returns ``None`` when the PR is not in DB.
    """
    try:
        async with get_session() as session:
            pr_result = await session.execute(
                select(ACPullRequest).where(
                    ACPullRequest.repo == repo,
                    ACPullRequest.github_number == number,
                )
            )
            pr = pr_result.scalar_one_or_none()
            if pr is None:
                return None

            issue: ACIssue | None = None
            if pr.closes_issue_number is not None:
                issue_result = await session.execute(
                    select(ACIssue).where(
                        ACIssue.repo == repo,
                        ACIssue.github_number == pr.closes_issue_number,
                    )
                )
                issue = issue_result.scalar_one_or_none()

            runs_result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.pr_number == number)
                .order_by(ACAgentRun.spawned_at.desc())
                .limit(20)
            )
            runs = runs_result.scalars().all()

        labels = json.loads(pr.labels_json)
        return PRDetailRow(
            number=pr.github_number,
            title=pr.title,
            state=pr.state,
            head_ref=pr.head_ref,
            labels=labels,
            closes_issue_number=pr.closes_issue_number,
            merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
            first_seen_at=pr.first_seen_at.isoformat(),
            last_synced_at=pr.last_synced_at.isoformat(),
            linked_issue=LinkedIssueRow(
                number=issue.github_number,
                title=issue.title,
                state=issue.state,
            ) if issue else None,
            agent_runs=[
                PRAgentRunRow(
                    id=r.id,
                    role=r.role,
                    status=r.status,
                    branch=r.branch,
                    issue_number=r.issue_number,
                    spawned_at=r.spawned_at.isoformat(),
                    last_activity_at=r.last_activity_at.isoformat() if r.last_activity_at else None,
                )
                for r in runs
            ],
        )
    except Exception as exc:
        logger.warning("⚠️  get_pr_detail DB query failed (non-fatal): %s", exc)
        return None


async def get_all_prs(
    repo: str,
    state: str | None = None,
    limit: int = 200,
) -> list[AllPRRow]:
    """Return PRs from ``pull_requests``, optionally filtered by state."""
    try:
        async with get_session() as session:
            stmt = (
                select(ACPullRequest)
                .where(ACPullRequest.repo == repo)
                .order_by(ACPullRequest.github_number.desc())
                .limit(limit)
            )
            if state:
                stmt = stmt.where(ACPullRequest.state == state)
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [
            AllPRRow(
                number=row.github_number,
                title=row.title,
                state=row.state,
                head_ref=row.head_ref,
                labels=json.loads(row.labels_json),
                closes_issue_number=row.closes_issue_number,
                merged_at=row.merged_at.isoformat() if row.merged_at else None,
                last_synced_at=row.last_synced_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_all_prs DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Wave aggregation from DB — fallback when filesystem worktrees are gone
# ---------------------------------------------------------------------------


async def get_waves_from_db(limit: int = 100) -> list[WaveRow]:
    """Return agent runs grouped by batch_id as wave-shaped dicts.

    Used by ``telemetry.aggregate_waves()`` when no ``.agent-task`` files exist
    on the filesystem (i.e. all worktrees have been cleaned up).  Groups rows
    in ``agent_runs`` by ``batch_id``, then shapes them into the same
    structure expected by ``WaveSummary`` so D3 charts work without changes.

    Returns dicts with keys: batch_id, started_at (UNIX float), ended_at
    (UNIX float | None), issues_worked (list[int]), prs_opened (int),
    agents (list[dict]).  Message counts default to 0 (no transcript data).
    """
    try:
        async with get_session() as session:
            stmt = (
                select(ACAgentRun)
                .order_by(ACAgentRun.spawned_at.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        # Group by batch_id.
        groups: dict[str, list[ACAgentRun]] = {}
        for row in rows:
            bid = row.batch_id or row.id  # lone runs get their own key
            groups.setdefault(bid, []).append(row)

        waves: list[WaveRow] = []
        for batch_id, members in groups.items():
            issues_worked = sorted(
                {r.issue_number for r in members if r.issue_number is not None}
            )
            prs_opened = sum(1 for r in members if r.pr_number is not None)
            started_ts = min(r.spawned_at for r in members).timestamp()
            completed = [r.completed_at for r in members if r.completed_at]
            ended_ts: float | None = (
                max(completed).timestamp() if len(completed) == len(members) and completed
                else None
            )

            agents: list[WaveAgentRow] = [
                WaveAgentRow(
                    id=r.id,
                    role=r.role,
                    status=r.status,
                    issue_number=r.issue_number,
                    pr_number=r.pr_number,
                    branch=r.branch,
                    batch_id=r.batch_id,
                    worktree_path=r.worktree_path,
                    cognitive_arch=r.cognitive_arch,
                    message_count=0,
                )
                for r in members
            ]

            waves.append(
                WaveRow(
                    batch_id=batch_id,
                    started_at=started_ts,
                    ended_at=ended_ts,
                    issues_worked=issues_worked,
                    prs_opened=prs_opened,
                    prs_merged=0,
                    estimated_tokens=0,
                    estimated_cost_usd=0.0,
                    agents=agents,
                )
            )

        # Most recent first.
        waves.sort(key=lambda w: w["started_at"], reverse=True)
        return waves
    except Exception as exc:
        logger.warning("⚠️  get_waves_from_db failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Counts for SSE expansion
# ---------------------------------------------------------------------------


async def get_closed_issues_count(repo: str, hours: int = 24) -> int:
    """Count issues closed within the last *hours* using the actual ``closed_at`` timestamp."""
    try:
        import datetime
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM issues "
                    "WHERE repo = :repo AND state = 'closed' AND closed_at >= :cutoff"
                ).bindparams(repo=repo, cutoff=cutoff)
            )
            row = result.one()
        return int(row[0])
    except Exception as exc:
        logger.warning("⚠️  get_closed_issues_count failed (non-fatal): %s", exc)
        return 0


async def get_merged_prs_count(repo: str, hours: int = 24) -> int:
    """Count PRs merged within the last *hours* using the actual ``merged_at`` timestamp."""
    try:
        import datetime
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM pull_requests "
                    "WHERE repo = :repo AND state = 'merged' AND merged_at >= :cutoff"
                ).bindparams(repo=repo, cutoff=cutoff)
            )
            row = result.one()
        return int(row[0])
    except Exception as exc:
        logger.warning("⚠️  get_merged_prs_count failed (non-fatal): %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Conductor spawn history
# ---------------------------------------------------------------------------


async def get_conductor_history(
    limit: int = 5,
    worktrees_dir: Path | None = None,
    host_worktrees_dir: Path | None = None,
) -> list[ConductorHistoryRow]:
    """Return the last *limit* conductor spawns with current active/completed status.

    Status is ``"active"`` when the worktree directory still exists on disk and
    ``"completed"`` once it has been removed.  Falls back to ``[]`` on any DB
    error so the UI degrades gracefully without surfacing the error to the user.
    """
    from sqlalchemy import desc

    from agentception.config import settings

    wt_dir = worktrees_dir or settings.worktrees_dir
    host_wt_dir = host_worktrees_dir or settings.host_worktrees_dir

    try:
        async with get_session() as session:
            stmt = (
                select(ACWave)
                .where(ACWave.role == "conductor")
                .order_by(desc(ACWave.started_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            waves = result.scalars().all()

        entries: list[ConductorHistoryRow] = []
        for wave in waves:
            worktree = Path(wt_dir) / wave.id
            host_worktree = Path(host_wt_dir) / wave.id
            entries.append(
                ConductorHistoryRow(
                    wave_id=wave.id,
                    worktree=str(worktree),
                    host_worktree=str(host_worktree),
                    started_at=wave.started_at.strftime("%Y-%m-%d %H:%M UTC"),
                    status="active" if worktree.exists() else "completed",
                )
            )
        return entries
    except Exception as exc:
        logger.warning("⚠️  get_conductor_history DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Build page — phase board, agent events, thoughts tail
# ---------------------------------------------------------------------------


class InitiativePhaseMeta(TypedDict):
    """Metadata for one phase of an initiative, read from ``initiative_phases``."""

    label: str
    """Scoped phase label, e.g. ``"ac-auth/0-foundation"``."""
    order: int
    """0-indexed canonical display position."""
    depends_on: list[str]
    """Scoped phase labels that must be complete before this phase unlocks."""


async def get_initiative_phase_meta(initiative: str) -> list[InitiativePhaseMeta]:
    """Return ordered phase metadata for an initiative from ``initiative_phases``.

    Returns rows sorted by ``phase_order ASC`` — this is the canonical display
    order.  Returns ``[]`` when no rows exist (initiative not yet filed or
    predates this feature); callers fall back to lexicographic sort in that case.

    Falls back to ``[]`` on DB error.
    """
    from agentception.db.models import ACInitiativePhase

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACInitiativePhase)
                .where(ACInitiativePhase.initiative == initiative)
                .order_by(ACInitiativePhase.phase_order)
            )
            rows = result.scalars().all()

        return [
            InitiativePhaseMeta(
                label=row.phase_label,
                order=row.phase_order,
                depends_on=json.loads(row.depends_on_json or "[]"),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_initiative_phase_meta failed (non-fatal): %s", exc)
        return []

# Labels that are never themselves initiative names — common GitHub system labels
# and AgentCeption internal labels.
_NON_INITIATIVE_LABELS = frozenset(
    {
        "enhancement", "bug", "documentation", "good first issue",
        "help wanted", "invalid", "question", "wontfix", "duplicate",
        "feature", "agent:wip", "priority:high", "priority:medium",
        "priority:low", "needs-triage", "in-progress", "review", "blocked",
    }
)

def _label_matches_patterns(label: str, patterns: list[str]) -> bool:
    """Return True if *label* matches any of the fnmatch-style *patterns*."""
    return any(fnmatch.fnmatch(label, pat) for pat in patterns)


class PhaseSummary(TypedDict):
    """A phase sub-label and its open-issue count, for the launch modal picker."""

    label: str
    count: int


class IssueSummary(TypedDict):
    """A minimal open-issue descriptor, for the launch modal single-issue picker."""

    number: int
    title: str


class LabelContext(TypedDict):
    """Data package returned by ``get_label_context`` to populate the launch modal."""

    phases: list[PhaseSummary]
    issues: list[IssueSummary]


async def get_label_context(repo: str, initiative_label: str) -> LabelContext:
    """Return phases and open issues for *initiative_label*, for the launch modal.

    *phases* — distinct sub-labels of the form ``<initiative>/<slug>`` that
    appear on at least one open issue, sorted by label name.

    *issues* — all open issues that carry *initiative_label* directly (i.e. the
    top-level label, not a sub-phase label), sorted by issue number ascending.

    Falls back to empty lists on DB error so the modal still opens gracefully.
    """
    phase_prefix = initiative_label + "/"
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    ACIssue.github_number,
                    ACIssue.title,
                    ACIssue.labels_json,
                    ACIssue.state,
                ).where(ACIssue.repo == repo, ACIssue.state == "open")
            )
            rows = result.all()

        phase_counts: dict[str, int] = {}
        issues: list[IssueSummary] = []

        for number, title, labels_json_str, _state in rows:
            labels: list[str] = json.loads(labels_json_str or "[]")
            has_initiative = initiative_label in labels

            # Collect phase sub-labels (e.g. "ac-workflow/5-plan-step-v2")
            for lbl in labels:
                if lbl.startswith(phase_prefix):
                    phase_counts[lbl] = phase_counts.get(lbl, 0) + 1

            # Collect issues directly tagged with the top-level initiative label
            if has_initiative:
                issues.append(IssueSummary(number=number, title=title or ""))

        phases: list[PhaseSummary] = sorted(
            [PhaseSummary(label=lbl, count=cnt) for lbl, cnt in phase_counts.items()],
            key=lambda p: p["label"],
        )
        issues.sort(key=lambda i: i["number"])

        return LabelContext(phases=phases, issues=issues)

    except Exception as exc:
        logger.warning("⚠️  get_label_context failed (non-fatal): %s", exc)
        return LabelContext(phases=[], issues=[])


async def get_initiatives(
    repo: str,
    initiative_patterns: list[str] | None = None,
) -> list[str]:
    """Return active initiative labels present in the DB, ordered by config position.

    When *initiative_patterns* is non-empty, a label is an initiative if it
    matches any of the fnmatch-style patterns (e.g. ``"ac-*"``, ``"agentception"``).
    Only labels that appear on at least one **open** issue are returned.

    The result order mirrors the order of *initiative_patterns*: a label whose
    first matching pattern appears earlier in the list sorts earlier.  This
    means the order declared in ``pipeline-config.json`` is the single source
    of truth for the initiative tab bar — no separate hardcoded list needed.

    When *initiative_patterns* is empty or ``None``, falls back to the legacy
    heuristic: a label is an initiative when it co-exists with a ``phase-N``
    label on the same issue and is not in ``_NON_INITIATIVE_LABELS``.

    Fully completed initiatives (all issues closed) are excluded from the tab
    bar to avoid noise over time.  Falls back to ``[]`` on DB error.
    """
    patterns: list[str] = initiative_patterns or []

    def _sort_key(label: str) -> tuple[int, str]:
        """Sort by first matching pattern index, then alphabetically."""
        for i, pat in enumerate(patterns):
            if fnmatch.fnmatch(label, pat):
                return (i, label)
        return (len(patterns), label)

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue.labels_json, ACIssue.state, ACIssue.phase_label)
                .where(ACIssue.repo == repo)
            )
            rows = result.all()

        initiative_states: dict[str, set[str]] = {}

        if patterns:
            # Config-driven path: a label matching a pattern is an initiative.
            # Only count issues that would actually be visible in the board —
            # an issue must have a scoped "{initiative}/phase-N" label; issues
            # without one are dropped by get_issues_grouped_by_phase and must
            # not cause a tab to appear.
            for labels_json_str, state, _phase_label in rows:
                labels: list[str] = json.loads(labels_json_str or "[]")
                matched_initiatives = [
                    lbl
                    for lbl in labels
                    # Scoped phase labels (e.g. "ac-build/phase-0") are never
                    # initiatives — the "/" is the canonical separator.
                    if "/" not in lbl and _label_matches_patterns(lbl, patterns)
                ]
                if not matched_initiatives:
                    continue

                # Issue must have at least one scoped "{initiative}/phase-N" label.
                has_scoped_phase = any(
                    any(lbl.startswith(f"{ini}/") for lbl in labels)
                    for ini in matched_initiatives
                )
                if not has_scoped_phase:
                    continue  # unphased issue — won't appear in board

                for lbl in matched_initiatives:
                    initiative_states.setdefault(lbl, set()).add(state or "open")
        else:
            # No patterns configured — return nothing (no legacy heuristic).
            pass

        # Only surface initiatives that still have at least one open issue,
        # ordered by their position in initiative_patterns then alphabetically.
        return sorted(
            (ini for ini, states in initiative_states.items() if "open" in states),
            key=_sort_key,
        )
    except Exception as exc:
        logger.warning("⚠️  get_initiatives DB query failed (non-fatal): %s", exc)
        return []


def _compute_locked(
    deps: list[str],
    complete_phases: set[str],
) -> bool:
    """Return True if any declared dependency is not yet complete.

    Empty *deps* → always unlocked (no stored dep graph, or phase-0 with no
    predecessors).
    """
    return any(dep not in complete_phases for dep in deps)


_MD_STRIP_RE = _re.compile(r"[#*`_\[\]!>|~]+|```[^`]*```|\n+")


def _body_excerpt(body: str | None, max_chars: int = 120) -> str:
    """Return a plain-text excerpt from a Markdown issue body.

    Strips common markdown syntax characters and collapses whitespace so the
    result reads as a short prose snippet suitable for a Kanban card subtitle.
    """
    if not isinstance(body, str) or not body:
        return ""
    text: str = str(_MD_STRIP_RE.sub(" ", body)).strip()
    if len(text) <= max_chars:
        return text
    # Break at the last word boundary within the limit.
    truncated = text[:max_chars]
    cut = truncated.rfind(" ")
    return (truncated[:cut] if cut > 0 else truncated) + "…"



async def get_issues_grouped_by_phase(
    repo: str,
    initiative: str | None = None,
    phase_order: list[str] | None = None,
) -> list[PhaseGroupRow]:
    """Return issues grouped by scoped phase label in canonical display order.

    Every issue is expected to carry two labels:
    - ``{initiative}``        — the initiative slug
    - ``{initiative}/{slug}`` — the namespaced phase identifier

    **Ordering priority** (when *initiative* is supplied):
    1. Explicitly passed *phase_order* list — for callers that know what they want.
    2. ``initiative_phases.phase_order`` from DB — canonical, written by
       ``file_issues``.  Returned sorted by ``phase_order ASC``.
    3. Lexicographic sort of discovered ``{initiative}/*`` labels — correct
       fallback for plans filed before migration 0010, provided labels follow
       the ``{N}-{slug}`` convention.
    4. Empty list — initiative exists but has no issues yet.  The board renders
       an empty state rather than phantom phase rows.

    When *initiative* is ``None`` the result spans all issues, grouped by
    whatever phase-like labels they carry, with no defined ordering (legacy).

    Returns ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue)
                .where(ACIssue.repo == repo)
                .order_by(ACIssue.github_number)
            )
            rows = result.scalars().all()

        groups: dict[str, list[PhasedIssueRow]] = {}
        for row in rows:
            issue_labels: list[str] = json.loads(row.labels_json or "[]")

            # Initiative filter: skip issues that don't carry this initiative label.
            if initiative and initiative not in issue_labels:
                continue

            # Phase key is the first scoped "{initiative}/*" label on the issue.
            phase_key: str | None = next(
                (lbl for lbl in issue_labels if initiative and lbl.startswith(f"{initiative}/")),
                None,
            )

            # Issues with no scoped phase label are silently dropped.
            if phase_key is None:
                continue

            issue_deps: list[int] = json.loads(row.depends_on_json or "[]")
            groups.setdefault(phase_key, []).append(
                PhasedIssueRow(
                    number=row.github_number,
                    title=row.title,
                    body_excerpt=_body_excerpt(row.body),
                    state=row.state,
                    url=f"https://github.com/{repo}/issues/{row.github_number}",
                    labels=issue_labels,
                    depends_on=issue_deps,
                )
            )

        # Determine effective display order and load dep graph.
        effective_phase_order: list[str]
        phase_deps: dict[str, list[str]] = {}

        if phase_order is not None:
            # Caller supplied an explicit ordering — use it as-is.
            effective_phase_order = phase_order
        elif initiative:
            meta = await get_initiative_phase_meta(initiative)
            if meta:
                # Canonical path: DB has stored order from file_issues.
                effective_phase_order = [m["label"] for m in meta]
                phase_deps = {m["label"]: m["depends_on"] for m in meta}
            elif groups:
                # Legacy fallback: derive order from actual label names.
                # Lexicographic sort is correct for the {N}-{slug} convention.
                effective_phase_order = sorted(groups.keys())
            else:
                # Initiative exists but has no issues filed yet.
                effective_phase_order = []
        else:
            effective_phase_order = []

        # Compute which phases are complete (all their issues closed).
        complete_phases: set[str] = set()
        for phase in effective_phase_order:
            phase_issues = groups.get(phase, [])
            if phase_issues and all(i["state"] == "closed" for i in phase_issues):
                complete_phases.add(phase)

        ordered: list[PhaseGroupRow] = []
        for phase in effective_phase_order:
            phase_issues = groups.pop(phase, [])
            deps = phase_deps.get(phase, [])
            ordered.append(
                PhaseGroupRow(
                    label=phase,
                    issues=phase_issues,
                    locked=_compute_locked(deps, complete_phases),
                    complete=phase in complete_phases,
                    depends_on=deps,
                )
            )

        if not initiative:
            # Legacy: append any remaining label buckets not covered by phase_order.
            for label, label_issues in groups.items():
                complete = bool(label_issues) and all(
                    i["state"] == "closed" for i in label_issues
                )
                ordered.append(
                    PhaseGroupRow(
                        label=label,
                        issues=label_issues,
                        locked=False,
                        complete=complete,
                        depends_on=[],
                    )
                )

        return ordered
    except Exception as exc:
        logger.warning("⚠️  get_issues_grouped_by_phase DB query failed (non-fatal): %s", exc)
        return []


def _compute_agent_status(
    status: str,
    last_activity_at: datetime.datetime | None,
) -> str:
    """Return a normalized, stale-aware status string for the build card badge.

    Maps DB status values to lower-case display strings and promotes active
    runs to ``"stale"`` when ``last_activity_at`` is older than
    ``_STALE_THRESHOLD_SECONDS``.
    """
    normalized = status.lower()
    if normalized in _ACTIVE_STATUSES and last_activity_at is not None:
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        # Ensure comparison works regardless of timezone-awareness.
        if last_activity_at.tzinfo is None:
            last_activity_at = last_activity_at.replace(tzinfo=datetime.timezone.utc)
        age_seconds = (now - last_activity_at).total_seconds()
        if age_seconds > _STALE_THRESHOLD_SECONDS:
            return "stale"
    return normalized


async def _get_step_data_for_runs(run_ids: list[str]) -> dict[str, _RunStepData]:
    """Return step-event summary keyed by run_id.

    Queries ``agent_events`` for ``step_start`` events and returns, per run:

    * ``current_step`` — step name from the most-recent event's JSON payload
      (``{"step": "<name>"}``); ``None`` if no events exist or payload is malformed.
    * ``steps_completed`` — total count of ``step_start`` events for the run.

    Falls back to ``{}`` on DB error (non-fatal).
    """
    if not run_ids:
        return {}
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    ACAgentEvent.agent_run_id,
                    ACAgentEvent.id,
                    ACAgentEvent.payload,
                )
                .where(
                    ACAgentEvent.agent_run_id.in_(run_ids),
                    ACAgentEvent.event_type == "step_start",
                )
                .order_by(ACAgentEvent.agent_run_id, ACAgentEvent.id)
            )
            raw_rows = result.all()

        # Group payloads by run_id (rows ordered by id ASC → last is latest).
        grouped: dict[str, list[str]] = {}
        for agent_run_id, _id, payload in raw_rows:
            if agent_run_id is None:
                continue
            grouped.setdefault(agent_run_id, []).append(payload or "{}")

        out: dict[str, _RunStepData] = {}
        for rid, payloads in grouped.items():
            try:
                latest_payload = json.loads(payloads[-1])
                step_raw = latest_payload.get("step")
                current_step: str | None = (
                    step_raw if isinstance(step_raw, str) else None
                )
            except (ValueError, AttributeError):
                current_step = None
            out[rid] = _RunStepData(
                current_step=current_step,
                steps_completed=len(payloads),
            )
        return out
    except Exception as exc:
        logger.warning("⚠️  _get_step_data_for_runs DB query failed (non-fatal): %s", exc)
        return {}


async def _get_active_reviewer_runs_for_prs(
    pr_numbers: list[int],
) -> dict[int, ACAgentRun]:
    """Return the most-recent active reviewer run keyed by PR number.

    Only returns runs whose tier is ``reviewer`` and whose status is one of the
    active statuses (``implementing``, ``pending_launch``, ``reviewing``).
    Falls back to ``{}`` on DB error.
    """
    if not pr_numbers:
        return {}
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(
                    ACAgentRun.pr_number.in_(pr_numbers),
                    ACAgentRun.tier == "reviewer",
                    ACAgentRun.status.in_(["implementing", "pending_launch", "reviewing"]),
                )
                .order_by(ACAgentRun.spawned_at.desc())
            )
            rows = result.scalars().all()

        out: dict[int, ACAgentRun] = {}
        for row in rows:
            if row.pr_number is None or row.pr_number in out:
                continue
            out[row.pr_number] = row
        return out
    except Exception as exc:
        logger.warning(
            "⚠️  _get_active_reviewer_runs_for_prs DB query failed (non-fatal): %s", exc
        )
        return {}


async def get_open_prs_by_issue(
    issue_numbers: list[int],
    repo: str,
) -> dict[int, OpenPRForIssueRow]:
    """Return open PRs keyed by associated issue number.

    Uses two matching signals (either is sufficient):
    1. ``closes_issue_number`` — explicit ``Closes #N`` link parsed from the PR body.
    2. ``head_ref`` matching ``feat/issue-{N}-*`` — branch naming convention.

    Only PRs with ``state == 'open'`` are returned.  This is the authoritative
    source for placing board cards into the ``pr_open`` or ``reviewing`` swim lane
    because it reads directly from the ``ac_pull_requests`` table (synced from
    GitHub) rather than from the fragile ``ac_agent_runs.pr_number`` column.

    Falls back to ``{}`` on DB error so the board degrades to live agent-run
    signals rather than crashing.
    """
    if not issue_numbers:
        return {}
    issue_set = set(issue_numbers)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACPullRequest).where(
                    ACPullRequest.repo == repo,
                    ACPullRequest.state == "open",
                )
            )
            rows = result.scalars().all()

        out: dict[int, OpenPRForIssueRow] = {}
        for row in rows:
            # Signal 1: explicit Closes #N link in PR body.
            if row.closes_issue_number is not None and row.closes_issue_number in issue_set:
                issue_num = row.closes_issue_number
                if issue_num not in out:
                    out[issue_num] = OpenPRForIssueRow(
                        pr_number=row.github_number,
                        head_ref=row.head_ref,
                    )
            # Signal 2: branch naming convention ac/issue-{N}.
            if row.head_ref:
                m = _AC_ISSUE_BRANCH_RE.match(row.head_ref)
                if m:
                    issue_num = int(m.group(1))
                    if issue_num in issue_set and issue_num not in out:
                        out[issue_num] = OpenPRForIssueRow(
                            pr_number=row.github_number,
                            head_ref=row.head_ref,
                        )
        return out
    except Exception as exc:
        logger.warning("⚠️  get_open_prs_by_issue DB query failed (non-fatal): %s", exc)
        return {}


async def get_workflow_states_by_issue(
    issue_numbers: list[int],
    repo: str,
) -> dict[int, WorkflowStateRow]:
    """Return canonical workflow state keyed by issue number.

    Reads from ``ac_issue_workflow_state`` — the persisted, canonical source
    of truth for swim lanes.  Falls back to ``{}`` on error so the board
    degrades gracefully.
    """
    if not issue_numbers:
        return {}
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssueWorkflowState).where(
                    ACIssueWorkflowState.repo == repo,
                    ACIssueWorkflowState.issue_number.in_(issue_numbers),
                )
            )
            rows = result.scalars().all()

        out: dict[int, WorkflowStateRow] = {}
        for row in rows:
            import json as _json
            warnings: list[str] = _json.loads(row.warnings_json or "[]")
            out[row.issue_number] = WorkflowStateRow(
                lane=row.lane,
                issue_state=row.issue_state,
                run_id=row.run_id,
                agent_status=row.agent_status,
                pr_number=row.pr_number,
                pr_state=row.pr_state,
                pr_base=row.pr_base,
                pr_head_ref=row.pr_head_ref,
                pr_link_method=row.pr_link_method,
                pr_link_confidence=row.pr_link_confidence,
                warnings=warnings,
            )
        return out
    except Exception as exc:
        logger.warning("⚠️  get_workflow_states_by_issue DB query failed (non-fatal): %s", exc)
        return {}


async def get_runs_for_issue_numbers(
    issue_numbers: list[int],
) -> dict[int, RunForIssueRow]:
    """Return the most-recent agent run keyed by issue number.

    Enriches each run with:

    * ``agent_status`` — normalized + stale-aware status string.  If an active
      reviewer run exists for the issue's PR, ``agent_status`` is promoted to
      ``"reviewing"`` so the board card moves into the Reviewing swim lane.
    * ``current_step`` — step name from the most-recent ``step_start`` event.
    * ``steps_completed`` — count of ``step_start`` events for the run.

    Only issue numbers that have at least one run are included in the result.
    Falls back to ``{}`` on DB error.
    """
    if not issue_numbers:
        return {}
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.issue_number.in_(issue_numbers))
                .order_by(ACAgentRun.spawned_at.desc())
            )
            rows = result.scalars().all()

        seen: set[int] = set()
        most_recent: dict[int, ACAgentRun] = {}
        for row in rows:
            if row.issue_number is None or row.issue_number in seen:
                continue
            seen.add(row.issue_number)
            most_recent[row.issue_number] = row

        # Fetch step-event data in a single query for all selected runs.
        run_ids = [r.id for r in most_recent.values()]
        step_data = await _get_step_data_for_runs(run_ids)

        # Check whether a QA reviewer run is actively reviewing any of these PRs.
        # Reviewer runs are associated with a PR number, not an issue number, so
        # they won't appear in most_recent — we detect them separately.
        pr_numbers = [r.pr_number for r in most_recent.values() if r.pr_number is not None]
        reviewer_runs = await _get_active_reviewer_runs_for_prs(pr_numbers)

        out: dict[int, RunForIssueRow] = {}
        for issue_num, row in most_recent.items():
            sd = step_data.get(row.id, _RunStepData(current_step=None, steps_completed=0))
            computed_status = _compute_agent_status(row.status, row.last_activity_at)
            # Promote to "reviewing" when an active reviewer is working on this issue's PR.
            if row.pr_number and row.pr_number in reviewer_runs:
                computed_status = "reviewing"
            out[issue_num] = RunForIssueRow(
                id=row.id,
                role=row.role,
                cognitive_arch=row.cognitive_arch,
                status=row.status,
                agent_status=computed_status,
                pr_number=row.pr_number,
                branch=row.branch,
                spawned_at=row.spawned_at.isoformat(),
                last_activity_at=(
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
                current_step=sd["current_step"],
                steps_completed=sd["steps_completed"],
                tier=row.tier,
                org_domain=row.org_domain,
                batch_id=row.batch_id,
            )
        return out
    except Exception as exc:
        logger.warning("⚠️  get_runs_for_issue_numbers DB query failed (non-fatal): %s", exc)
        return {}


async def get_run_tree_by_batch_id(batch_id: str) -> list[RunTreeNodeRow]:
    """Return all agent runs sharing *batch_id*, ordered by spawn time.

    The returned flat list can be assembled into a tree client-side by
    following ``parent_run_id`` references.  Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.batch_id == batch_id)
                .order_by(ACAgentRun.spawned_at.asc())
            )
            rows = result.scalars().all()

        run_ids = [r.id for r in rows]
        step_data = await _get_step_data_for_runs(run_ids)

        out: list[RunTreeNodeRow] = []
        for row in rows:
            sd = step_data.get(row.id, _RunStepData(current_step=None, steps_completed=0))
            out.append(
                RunTreeNodeRow(
                    id=row.id,
                    role=row.role,
                    status=row.status,
                    agent_status=_compute_agent_status(row.status, row.last_activity_at),
                    tier=row.tier,
                    org_domain=row.org_domain,
                    parent_run_id=row.parent_run_id,
                    issue_number=row.issue_number,
                    pr_number=row.pr_number,
                    batch_id=row.batch_id,
                    spawned_at=row.spawned_at.isoformat(),
                    last_activity_at=(
                        row.last_activity_at.isoformat() if row.last_activity_at else None
                    ),
                    current_step=sd["current_step"],
                )
            )
        return out
    except Exception as exc:
        logger.warning("⚠️  get_run_tree_by_batch_id DB query failed (non-fatal): %s", exc)
        return []


async def get_latest_active_batch_id(
    issue_numbers: list[int] | None = None,
) -> str | None:
    """Return the batch_id of the most recently active run.

    Returns a ``batch_id`` only when at least one run has a genuinely live
    status (``implementing``, ``pending_launch``, or ``reviewing``).  Returns
    ``None`` when no live runs exist so the hierarchy panel renders an empty
    state rather than showing stale data from a previous or unrelated dispatch.

    When *issue_numbers* is provided the query is restricted to runs whose
    ``issue_number`` is in that set.  Callers pass only open issue numbers
    for the current initiative to prevent cross-initiative contamination and
    closed issues from surfacing ghost agents in the hierarchy panel.
    """
    try:
        async with get_session() as session:
            query = (
                select(ACAgentRun.batch_id)
                .where(
                    ACAgentRun.batch_id.is_not(None),
                    ACAgentRun.status.in_(list(_LIVE_STATUSES)),
                )
            )
            if issue_numbers:
                query = query.where(ACAgentRun.issue_number.in_(issue_numbers))
            query = query.order_by(ACAgentRun.spawned_at.desc()).limit(1)
            result = await session.execute(query)
            row = result.scalar_one_or_none()
            return str(row) if row else None
    except Exception as exc:
        logger.warning("⚠️  get_latest_active_batch_id DB query failed (non-fatal): %s", exc)
        return None


async def get_pending_launches() -> list[PendingLaunchRow]:
    """Return all agent runs with ``status='pending_launch'``, oldest first.

    Each dict contains everything the coordinator needs to claim the run and
    spawn a worker Task: run_id, issue_number, role, branch, worktree paths,
    batch_id, and the AC callback URL hint.

    Falls back to ``[]`` on DB error.
    """
    import json as _json

    logger.warning("🗄️  get_pending_launches: opening DB session")
    try:
        async with get_session() as session:
            logger.warning("🗄️  get_pending_launches: executing SELECT WHERE status='pending_launch'")
            result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.status == "pending_launch")
                .order_by(ACAgentRun.spawned_at.asc())
            )
            rows = result.scalars().all()
            logger.warning("🗄️  get_pending_launches: query returned %d raw row(s)", len(rows))
            for row in rows:
                logger.warning(
                    "🗄️    raw row: id=%r status=%r role=%r spawn_mode=%r",
                    row.id, row.status, row.role, row.spawn_mode,
                )

        launches: list[PendingLaunchRow] = []
        for row in rows:
            # host_worktree is stashed in spawn_mode as JSON by persist_agent_run_dispatch
            host_worktree: str | None = None
            if row.spawn_mode:
                try:
                    meta = _json.loads(row.spawn_mode)
                    host_worktree = meta.get("host_worktree")
                except (ValueError, AttributeError) as parse_exc:
                    logger.warning(
                        "⚠️  get_pending_launches: could not parse spawn_mode for %r: %s",
                        row.id, parse_exc,
                    )
            launches.append(
                PendingLaunchRow(
                    run_id=row.id,
                    issue_number=row.issue_number,
                    role=row.role,
                    branch=row.branch,
                    worktree_path=row.worktree_path,
                    host_worktree_path=host_worktree,
                    batch_id=row.batch_id,
                    spawned_at=row.spawned_at.isoformat(),
                    tier=row.tier,
                    org_domain=row.org_domain,
                    parent_run_id=row.parent_run_id,
                )
            )
        logger.warning("🗄️  get_pending_launches: returning %d launch(es)", len(launches))
        return launches
    except Exception as exc:
        logger.warning("❌ get_pending_launches DB query FAILED: %s", exc, exc_info=True)
        return []


async def get_agent_events_tail(
    run_id: str,
    after_id: int = 0,
) -> list[AgentEventRow]:
    """Return MCP-reported events for *run_id* with ``id > after_id``.

    Used by the inspector SSE stream to incrementally push new events.
    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentEvent)
                .where(
                    ACAgentEvent.agent_run_id == run_id,
                    ACAgentEvent.id > after_id,
                )
                .order_by(ACAgentEvent.id)
            )
            rows = result.scalars().all()

        return [
            AgentEventRow(
                id=row.id,
                event_type=row.event_type,
                payload=row.payload or "{}",
                recorded_at=row.recorded_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_events_tail DB query failed (non-fatal): %s", exc)
        return []


async def get_prs_grouped_by_phase(
    repo: str,
    initiative: str | None = None,
    batch_id: str | None = None,
    limit: int = 200,
) -> list[ShipPhaseGroupRow]:
    """Return PRs grouped by phase label for the Ship board.

    Each PR is matched to its closing issue (via ``closes_issue_number``) to
    determine the phase and initiative membership.  Each PR is enriched with
    the latest ``pr-reviewer`` agent run.

    When *initiative* is supplied, only PRs whose closing issue (or the PR
    itself) carries that initiative label are included.

    When *batch_id* is supplied, only PRs associated with agent runs from
    that batch are included (looked up via ``ac_agent_runs.batch_id``).

    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            pr_result = await session.execute(
                select(ACPullRequest)
                .where(ACPullRequest.repo == repo)
                .order_by(ACPullRequest.github_number.desc())
                .limit(limit)
            )
            prs = pr_result.scalars().all()

            issue_result = await session.execute(
                select(ACIssue).where(ACIssue.repo == repo)
            )
            issues: dict[int, ACIssue] = {
                row.github_number: row for row in issue_result.scalars().all()
            }

            reviewer_run_result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.role == "pr-reviewer")
                .order_by(ACAgentRun.spawned_at.desc())
            )
            all_reviewer_run_rows = reviewer_run_result.scalars().all()

            # Build batch filter sets when batch_id is specified.
            batch_pr_numbers: set[int] | None = None
            batch_issue_numbers: set[int] = set()
            if batch_id:
                batch_run_result = await session.execute(
                    select(ACAgentRun).where(ACAgentRun.batch_id == batch_id)
                )
                batch_runs = batch_run_result.scalars().all()
                batch_pr_numbers = set()
                for run in batch_runs:
                    if run.pr_number is not None:
                        batch_pr_numbers.add(run.pr_number)
                    if run.issue_number is not None:
                        batch_issue_numbers.add(run.issue_number)

        # Latest reviewer run per PR number (already ordered by spawned_at desc).
        reviewer_runs: dict[int, ACAgentRun] = {}
        for run in all_reviewer_run_rows:
            if run.pr_number is not None and run.pr_number not in reviewer_runs:
                reviewer_runs[run.pr_number] = run

        # Group PRs by phase.
        groups: dict[str, list[ShipPRRow]] = {}

        for pr in prs:
            pr_labels: list[str] = json.loads(pr.labels_json or "[]")

            # Batch filter: include only PRs linked to this batch.
            if batch_pr_numbers is not None:
                pr_in_batch = pr.github_number in batch_pr_numbers or (
                    pr.closes_issue_number is not None
                    and pr.closes_issue_number in batch_issue_numbers
                )
                if not pr_in_batch:
                    continue

            # Resolve phase and initiative from closing issue when available.
            phase_label: str | None = None
            if pr.closes_issue_number is not None:
                linked_issue = issues.get(pr.closes_issue_number)
                if linked_issue is not None:
                    issue_labels: list[str] = json.loads(linked_issue.labels_json or "[]")
                    if initiative and initiative not in issue_labels:
                        continue
                    phase_label = next(
                        (lbl for lbl in issue_labels if lbl.startswith("phase-")),
                        None,
                    ) or linked_issue.phase_label
                else:
                    if initiative and initiative not in pr_labels:
                        continue
            else:
                if initiative and initiative not in pr_labels:
                    continue
                phase_label = next(
                    (lbl for lbl in pr_labels if lbl.startswith("phase-")),
                    None,
                )

            phase_key = phase_label or "unphased"

            reviewer_run_row = reviewer_runs.get(pr.github_number)
            ship_pr = ShipPRRow(
                number=pr.github_number,
                title=pr.title,
                state=pr.state,
                head_ref=pr.head_ref,
                url=f"https://github.com/{repo}/pull/{pr.github_number}",
                labels=pr_labels,
                closes_issue_number=pr.closes_issue_number,
                merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                phase_label=phase_label,
                reviewer_run=ShipReviewerRunRow(
                    id=reviewer_run_row.id,
                    status=reviewer_run_row.status,
                    spawned_at=reviewer_run_row.spawned_at.isoformat(),
                    last_activity_at=(
                        reviewer_run_row.last_activity_at.isoformat()
                        if reviewer_run_row.last_activity_at
                        else None
                    ),
                ) if reviewer_run_row else None,
            )
            groups.setdefault(phase_key, []).append(ship_pr)

        # Emit phases in natural sort order; "unphased" bucket last.
        result: list[ShipPhaseGroupRow] = []
        for phase in sorted(k for k in groups if k != "unphased"):
            result.append(ShipPhaseGroupRow(label=phase, prs=groups[phase]))
        if "unphased" in groups:
            result.append(ShipPhaseGroupRow(label="unphased", prs=groups["unphased"]))
        return result
    except Exception as exc:
        logger.warning("⚠️  get_prs_grouped_by_phase DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_thoughts_tail(
    run_id: str,
    after_seq: int = -1,
    roles: tuple[str, ...] = ("thinking", "assistant"),
) -> list[AgentThoughtRow]:
    """Return transcript messages for *run_id* with ``sequence_index > after_seq``.

    Defaults to thinking + assistant messages — the raw chain-of-thought stream
    captured from Cursor transcripts by the poller.  Falls back to ``[]`` on error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentMessage)
                .where(
                    ACAgentMessage.agent_run_id == run_id,
                    ACAgentMessage.sequence_index > after_seq,
                    ACAgentMessage.role.in_(list(roles)),
                )
                .order_by(ACAgentMessage.sequence_index)
                .limit(50)
            )
            rows = result.scalars().all()

        return [
            AgentThoughtRow(
                seq=row.sequence_index,
                role=row.role,
                content=row.content or "",
                recorded_at=row.recorded_at.isoformat(),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_thoughts_tail DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_run_teardown(run_id: str) -> AgentRunTeardownRow | None:
    """Return the worktree path and branch for a single agent run.

    Used by ``report_done`` to clean up the worktree and remote branch without
    fetching the full run detail (transcript, messages, etc.).
    Returns ``None`` when the run does not exist or the DB query fails.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.worktree_path, ACAgentRun.branch).where(
                    ACAgentRun.id == run_id
                )
            )
            row = result.one_or_none()
        if row is None:
            return None
        return AgentRunTeardownRow(
            worktree_path=row.worktree_path,
            branch=row.branch,
        )
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_teardown DB query failed (non-fatal): %s", exc)
        return None
