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
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select, text

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentEvent,
    ACAgentMessage,
    ACAgentRun,
    ACIssue,
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
    node_type: str | None
    logical_tier: str | None
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
    messages: list[AgentMessageRow]


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
    state: str
    url: str
    labels: list[str]


class PhaseGroupRow(TypedDict):
    """One phase bucket from get_issues_grouped_by_phase."""

    label: str
    issues: list[PhasedIssueRow]
    locked: bool
    complete: bool
    depends_on: list[str]


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
    steps_total: int | None


# ---------------------------------------------------------------------------
# Step-data helpers — used internally by get_runs_for_issue_numbers
# ---------------------------------------------------------------------------

#: A run in an active status with no event newer than this is marked "stale".
_STALE_THRESHOLD_SECONDS: int = 1800  # 30 minutes

#: Statuses considered "active" for staleness detection.
_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"implementing", "pending_launch", "reviewing"}
)


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
    node_type: str | None
    logical_tier: str | None
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
                node_type=row.node_type,
                logical_tier=row.logical_tier,
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


_PHASE_ORDER = ["phase-0", "phase-1", "phase-2", "phase-3"]


async def get_initiative_phase_deps(initiative: str) -> dict[str, list[str]]:
    """Return the phase dependency graph for an initiative.

    Returns ``{phase_label: [dep_phase_label, ...]}`` — each key is a phase,
    each value is the list of phases that must be fully closed before this
    phase is unlocked.

    Returns ``{}`` (no deps — all phases unlocked) when the initiative has no
    stored dep graph.  This is the correct default for initiatives created
    before ``initiative_phases`` was introduced.

    Falls back to ``{}`` on DB error.
    """
    from agentception.db.models import ACInitiativePhase

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACInitiativePhase).where(
                    ACInitiativePhase.initiative == initiative
                )
            )
            rows = result.scalars().all()

        return {
            row.phase_label: json.loads(row.depends_on_json or "[]")
            for row in rows
        }
    except Exception as exc:
        logger.warning("⚠️  get_initiative_phase_deps failed (non-fatal): %s", exc)
        return {}

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
                select(ACIssue.labels_json, ACIssue.state).where(ACIssue.repo == repo)
            )
            rows = result.all()

        initiative_states: dict[str, set[str]] = {}

        if patterns:
            # Config-driven path: any label matching a pattern is an initiative.
            for labels_json_str, state in rows:
                labels: list[str] = json.loads(labels_json_str or "[]")
                for lbl in labels:
                    if _label_matches_patterns(lbl, patterns):
                        initiative_states.setdefault(lbl, set()).add(state or "open")
        else:
            # Legacy heuristic: issue must carry a phase-N label; sibling
            # labels (not phase-N, not in blocklist) are the initiatives.
            for labels_json_str, state in rows:
                labels = json.loads(labels_json_str or "[]")
                if not any(lbl.startswith("phase-") for lbl in labels):
                    continue
                for lbl in labels:
                    if not lbl.startswith("phase-") and lbl not in _NON_INITIATIVE_LABELS:
                        initiative_states.setdefault(lbl, set()).add(state or "open")

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
    phase_label: str,
    deps: list[str],
    complete_phases: set[str],
) -> bool:
    """Return True if any declared dependency is not yet complete.

    When *deps* is empty (no dependency data stored) the phase is always
    unlocked — the correct default for plans without stored dep graphs.
    """
    return any(dep not in complete_phases for dep in deps)


async def get_issues_grouped_by_phase(
    repo: str,
    initiative: str | None = None,
    phase_order: list[str] | None = None,
) -> list[PhaseGroupRow]:
    """Return issues grouped by phase, ordered according to *phase_order*.

    *phase_order* is the caller-supplied list of phase label strings that
    defines which phases appear in the result and in what order.  When
    omitted it falls back to ``_PHASE_ORDER`` (``["phase-0".."phase-3"]``).
    Pass ``settings.active_labels_order`` from ``pipeline-config.json`` to
    make the Build board reflect the project configuration rather than the
    hard-coded default.

    When *initiative* is supplied the result is scoped to that initiative:
    - Only issues carrying that initiative label are included.
    - Every phase in *phase_order* is present in the result (even if empty)
      so the UI can render the full gate structure.
    - No ``"unphased"`` bucket is emitted.

    When *initiative* is ``None`` the result spans all issues: configured
    phases first, then remaining label buckets, then ``"unphased"``.

    Each group dict contains:
    - ``label``    — phase label string
    - ``issues``   — list of issue dicts (number, title, state, url, labels)
    - ``locked``   — True when the preceding phase still has open issues
    - ``complete`` — True when every issue in this phase is closed

    Falls back to ``[]`` on DB error.
    """
    effective_phase_order: list[str] = phase_order if phase_order else _PHASE_ORDER
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue)
                .where(ACIssue.repo == repo)
                .order_by(ACIssue.github_number)
            )
            rows = result.scalars().all()

        # Group by phase label.
        # Prefer a generic "phase-N" label (old-style), then an initiative-
        # scoped "{initiative}/{phase-name}" label (new-style), then
        # row.phase_label (set at poll time), then "unphased".
        groups: dict[str, list[PhasedIssueRow]] = {}
        for row in rows:
            issue_labels: list[str] = json.loads(row.labels_json or "[]")

            # Initiative filter: skip issues that don't carry this initiative.
            if initiative and initiative not in issue_labels:
                continue

            phase_key: str | None = next(
                (lbl for lbl in issue_labels if lbl.startswith("phase-")),
                None,
            )
            if phase_key is None and initiative:
                # New-style: "agentception-ux-phase1b-to-phase3/2-ux-impl"
                phase_key = next(
                    (lbl for lbl in issue_labels if lbl.startswith(f"{initiative}/")),
                    None,
                )
            phase_key = phase_key or row.phase_label or "unphased"

            # In initiative-scoped mode skip the unphased bucket entirely.
            if initiative and phase_key == "unphased":
                continue

            groups.setdefault(phase_key, []).append(
                PhasedIssueRow(
                    number=row.github_number,
                    title=row.title,
                    state=row.state,
                    url=f"https://github.com/{repo}/issues/{row.github_number}",
                    labels=issue_labels,
                )
            )

        # When initiative-scoped, check whether the configured phase_order
        # actually belongs to this initiative.  If none of the config phases
        # appear in the groups dict (e.g. config still has ac-ui/* but the
        # selected initiative is agentception-ux-*), derive the order from
        # the actual group labels so the board isn't blank.
        if initiative and not any(p in groups for p in effective_phase_order):
            effective_phase_order = sorted(groups.keys())

        # Load the phase dependency graph for this initiative.
        # Empty dict → no deps stored → all phases unlocked (correct default).
        phase_deps: dict[str, list[str]] = {}
        if initiative:
            phase_deps = await get_initiative_phase_deps(initiative)

        # Build ordered list; compute complete set first so we can evaluate
        # deps in a single pass.
        complete_phases: set[str] = set()
        for phase in effective_phase_order:
            issues = groups.get(phase, [])
            if bool(issues) and all(i["state"] == "closed" for i in issues):
                complete_phases.add(phase)

        ordered: list[PhaseGroupRow] = []
        for phase in effective_phase_order:
            issues = groups.pop(phase, [])
            complete = phase in complete_phases
            deps = phase_deps.get(phase, [])
            locked = _compute_locked(phase, deps, complete_phases)
            ordered.append(
                PhaseGroupRow(
                    label=phase,
                    issues=issues,
                    locked=locked,
                    complete=complete,
                    depends_on=deps,
                )
            )

        if not initiative:
            # Legacy: append remaining label buckets, then unphased.
            for label, issues in groups.items():
                complete = bool(issues) and all(i["state"] == "closed" for i in issues)
                ordered.append(
                    PhaseGroupRow(
                        label=label,
                        issues=issues,
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


async def get_runs_for_issue_numbers(
    issue_numbers: list[int],
) -> dict[int, RunForIssueRow]:
    """Return the most-recent agent run keyed by issue number.

    Enriches each run with:

    * ``agent_status`` — normalized + stale-aware status string.
    * ``current_step`` — step name from the most-recent ``step_start`` event.
    * ``steps_completed`` — count of ``step_start`` events for the run.
    * ``steps_total`` — always ``None``; the DB does not track planned step count.

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

        out: dict[int, RunForIssueRow] = {}
        for issue_num, row in most_recent.items():
            sd = step_data.get(row.id, _RunStepData(current_step=None, steps_completed=0))
            out[issue_num] = RunForIssueRow(
                id=row.id,
                role=row.role,
                cognitive_arch=row.cognitive_arch,
                status=row.status,
                agent_status=_compute_agent_status(row.status, row.last_activity_at),
                pr_number=row.pr_number,
                branch=row.branch,
                spawned_at=row.spawned_at.isoformat(),
                last_activity_at=(
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
                current_step=sd["current_step"],
                steps_completed=sd["steps_completed"],
                steps_total=None,
            )
        return out
    except Exception as exc:
        logger.warning("⚠️  get_runs_for_issue_numbers DB query failed (non-fatal): %s", exc)
        return {}


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
                    node_type=row.node_type,
                    logical_tier=row.logical_tier,
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
