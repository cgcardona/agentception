from __future__ import annotations

"""Read-only query helpers for AgentCeption's Postgres data store.

All functions return plain dicts / lists so callers (routes, poller) have
zero dependency on SQLAlchemy internals.  Swallows DB errors and returns
empty results so a database outage degrades gracefully to in-memory state.
"""

import json
import logging
from typing import Any

from sqlalchemy import select, text

from pathlib import Path

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Board issues — replaces live gh CLI call in the overview sidebar
# ---------------------------------------------------------------------------


async def get_board_issues(
    repo: str,
    label: str | None = None,
    include_claimed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return open issues from ``ac_issues``, optionally filtered by phase label.

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

        issues: list[dict[str, Any]] = []
        for row in rows:
            labels = json.loads(row.labels_json)
            is_claimed = "agent:wip" in labels
            if not include_claimed and is_claimed:
                continue
            issues.append(
                {
                    "number": row.github_number,
                    "title": row.title,
                    "state": row.state,
                    "labels": [{"name": n} for n in labels],
                    "claimed": is_claimed,
                    "phase_label": row.phase_label,
                    "last_synced_at": row.last_synced_at.isoformat(),
                }
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
) -> list[dict[str, Any]]:
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
            {
                "polled_at": row.polled_at.isoformat(),
                "active_label": row.active_label,
                "issues_open": row.issues_open,
                "prs_open": row.prs_open,
                "agents_active": row.agents_active,
                "alert_count": len(json.loads(row.alerts_json)),
            }
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
) -> list[dict[str, Any]]:
    """Return recent agent runs from ``ac_agent_runs``, newest first."""
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
            {
                "id": row.id,
                "wave_id": row.wave_id,
                "issue_number": row.issue_number,
                "pr_number": row.pr_number,
                "branch": row.branch,
                "worktree_path": row.worktree_path,
                "role": row.role,
                "status": row.status,
                "attempt_number": row.attempt_number,
                "spawn_mode": row.spawn_mode,
                "batch_id": row.batch_id,
                "spawned_at": row.spawned_at.isoformat(),
                "last_activity_at": (
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
                "completed_at": (
                    row.completed_at.isoformat() if row.completed_at else None
                ),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_history DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_run_detail(
    run_id: str,
) -> dict[str, Any] | None:
    """Return a single agent run with its transcript messages.

    ``run_id`` is the worktree basename (e.g. ``issue-732``), which is the
    primary key stored in ``ac_agent_runs``.
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

        return {
            "id": run.id,
            "issue_number": run.issue_number,
            "pr_number": run.pr_number,
            "branch": run.branch,
            "role": run.role,
            "status": run.status,
            "spawned_at": run.spawned_at.isoformat(),
            "last_activity_at": (
                run.last_activity_at.isoformat() if run.last_activity_at else None
            ),
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tool_name": m.tool_name,
                    "sequence_index": m.sequence_index,
                    "recorded_at": m.recorded_at.isoformat(),
                }
                for m in messages
            ],
        }
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_detail DB query failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Open PRs — replaces gh CLI PR reads
# ---------------------------------------------------------------------------


async def get_open_prs_db(repo: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return open PRs from ``ac_pull_requests``."""
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
            {
                "number": row.github_number,
                "title": row.title,
                "state": row.state,
                "headRefName": row.head_ref,
                "labels": [{"name": n} for n in json.loads(row.labels_json)],
            }
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
) -> dict[str, Any] | None:
    """Return full detail for a single issue from ``ac_issues``.

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
        return {
            "number": issue.github_number,
            "title": issue.title,
            "body": issue.body or "",
            "state": issue.state,
            "labels": labels,
            "phase_label": issue.phase_label,
            "claimed": "agent:wip" in labels,
            "first_seen_at": issue.first_seen_at.isoformat(),
            "last_synced_at": issue.last_synced_at.isoformat(),
            "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
            "linked_prs": [
                {
                    "number": pr.github_number,
                    "title": pr.title,
                    "state": pr.state,
                    "head_ref": pr.head_ref,
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                }
                for pr in linked_prs
            ],
            "agent_runs": [
                {
                    "id": r.id,
                    "role": r.role,
                    "status": r.status,
                    "branch": r.branch,
                    "pr_number": r.pr_number,
                    "spawned_at": r.spawned_at.isoformat(),
                    "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                }
                for r in runs
            ],
        }
    except Exception as exc:
        logger.warning("⚠️  get_issue_detail DB query failed (non-fatal): %s", exc)
        return None


async def get_all_issues(
    repo: str,
    state: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return issues from ``ac_issues``, optionally filtered by state."""
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
            {
                "number": row.github_number,
                "title": row.title,
                "state": row.state,
                "labels": json.loads(row.labels_json),
                "phase_label": row.phase_label,
                "closed_at": row.closed_at.isoformat() if row.closed_at else None,
                "last_synced_at": row.last_synced_at.isoformat(),
            }
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
) -> dict[str, Any] | None:
    """Return full detail for a single PR from ``ac_pull_requests``.

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
        return {
            "number": pr.github_number,
            "title": pr.title,
            "state": pr.state,
            "head_ref": pr.head_ref,
            "labels": labels,
            "closes_issue_number": pr.closes_issue_number,
            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            "first_seen_at": pr.first_seen_at.isoformat(),
            "last_synced_at": pr.last_synced_at.isoformat(),
            "linked_issue": {
                "number": issue.github_number,
                "title": issue.title,
                "state": issue.state,
            } if issue else None,
            "agent_runs": [
                {
                    "id": r.id,
                    "role": r.role,
                    "status": r.status,
                    "branch": r.branch,
                    "issue_number": r.issue_number,
                    "spawned_at": r.spawned_at.isoformat(),
                    "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                }
                for r in runs
            ],
        }
    except Exception as exc:
        logger.warning("⚠️  get_pr_detail DB query failed (non-fatal): %s", exc)
        return None


async def get_all_prs(
    repo: str,
    state: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return PRs from ``ac_pull_requests``, optionally filtered by state."""
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
            {
                "number": row.github_number,
                "title": row.title,
                "state": row.state,
                "head_ref": row.head_ref,
                "labels": json.loads(row.labels_json),
                "closes_issue_number": row.closes_issue_number,
                "merged_at": row.merged_at.isoformat() if row.merged_at else None,
                "last_synced_at": row.last_synced_at.isoformat(),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_all_prs DB query failed (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Wave aggregation from DB — fallback when filesystem worktrees are gone
# ---------------------------------------------------------------------------


async def get_waves_from_db(limit: int = 100) -> list[dict[str, Any]]:
    """Return agent runs grouped by batch_id as wave-shaped dicts.

    Used by ``telemetry.aggregate_waves()`` when no ``.agent-task`` files exist
    on the filesystem (i.e. all worktrees have been cleaned up).  Groups rows
    in ``ac_agent_runs`` by ``batch_id``, then shapes them into the same
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
        groups: dict[str, list[Any]] = {}
        for row in rows:
            bid = row.batch_id or row.id  # lone runs get their own key
            groups.setdefault(bid, []).append(row)

        waves: list[dict[str, Any]] = []
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

            agents = [
                {
                    "id": r.id,
                    "role": r.role,
                    "status": r.status,
                    "issue_number": r.issue_number,
                    "pr_number": r.pr_number,
                    "branch": r.branch,
                    "batch_id": r.batch_id,
                    "worktree_path": r.worktree_path,
                    "cognitive_arch": None,
                    "message_count": 0,
                }
                for r in members
            ]

            waves.append(
                {
                    "batch_id": batch_id,
                    "started_at": started_ts,
                    "ended_at": ended_ts,
                    "issues_worked": issues_worked,
                    "prs_opened": prs_opened,
                    "prs_merged": 0,
                    "estimated_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "agents": agents,
                }
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
                    "SELECT COUNT(*) FROM ac_issues "
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
                    "SELECT COUNT(*) FROM ac_pull_requests "
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
) -> list[dict[str, Any]]:
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

        entries: list[dict[str, Any]] = []
        for wave in waves:
            worktree = Path(wt_dir) / wave.id
            host_worktree = Path(host_wt_dir) / wave.id
            entries.append(
                {
                    "wave_id": wave.id,
                    "worktree": str(worktree),
                    "host_worktree": str(host_worktree),
                    "started_at": wave.started_at.strftime("%Y-%m-%d %H:%M UTC"),
                    "status": "active" if worktree.exists() else "completed",
                }
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
    before ``ac_initiative_phases`` was introduced.

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


async def get_initiatives(repo: str) -> list[str]:
    """Return alphabetically sorted initiative labels present in the DB.

    An "initiative" label is any GitHub label attached to an issue that:
    - also carries at least one ``phase-N`` label (new-format issues only)
    - is not itself a ``phase-N`` label
    - is not in ``_NON_INITIATIVE_LABELS``

    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue.labels_json).where(ACIssue.repo == repo)
            )
            rows = result.scalars().all()

        found: set[str] = set()
        for labels_json_str in rows:
            labels: list[str] = json.loads(labels_json_str or "[]")
            if not any(lbl.startswith("phase-") for lbl in labels):
                continue
            for lbl in labels:
                if not lbl.startswith("phase-") and lbl not in _NON_INITIATIVE_LABELS:
                    found.add(lbl)

        return sorted(found)
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
) -> list[dict[str, Any]]:
    """Return issues grouped by phase, ordered phase-0..3.

    When *initiative* is supplied the result is scoped to that initiative:
    - Only issues carrying that initiative label are included.
    - All four phases are always present in the result (even if empty) so
      the UI can render the full gate structure.
    - No ``"unphased"`` bucket is emitted.

    When *initiative* is ``None`` the legacy behaviour is preserved:
    phase-0..3 first, then remaining label buckets, then ``"unphased"``.

    Each group dict contains:
    - ``label``    — phase label string
    - ``issues``   — list of issue dicts (number, title, state, url, labels)
    - ``locked``   — True when the preceding phase still has open issues
    - ``complete`` — True when every issue in this phase is closed

    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue)
                .where(ACIssue.repo == repo)
                .order_by(ACIssue.github_number)
            )
            rows = result.scalars().all()

        # Group by phase label.
        # Prefer a "phase-N" GitHub label from labels_json (works for any
        # initiative) over the legacy phase_label column (which was set from
        # the pipeline's active_label at poll time and is often wrong or None
        # for issues created outside the active pipeline window).
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            issue_labels: list[str] = json.loads(row.labels_json or "[]")

            # Initiative filter: skip issues that don't carry this initiative.
            if initiative and initiative not in issue_labels:
                continue

            phase_key = next(
                (lbl for lbl in issue_labels if lbl.startswith("phase-")),
                None,
            ) or row.phase_label or "unphased"

            # In initiative-scoped mode skip the unphased bucket entirely.
            if initiative and phase_key == "unphased":
                continue

            groups.setdefault(phase_key, []).append(
                {
                    "number": row.github_number,
                    "title": row.title,
                    "state": row.state,
                    "url": f"https://github.com/{repo}/issues/{row.github_number}",
                    "labels": issue_labels,
                }
            )

        # Load the phase dependency graph for this initiative.
        # Empty dict → no deps stored → all phases unlocked (correct default).
        phase_deps: dict[str, list[str]] = {}
        if initiative:
            phase_deps = await get_initiative_phase_deps(initiative)

        # Build ordered list; compute complete set first so we can evaluate
        # deps in a single pass.
        complete_phases: set[str] = set()
        for phase in _PHASE_ORDER:
            issues = groups.get(phase, [])
            if bool(issues) and all(i["state"] == "closed" for i in issues):
                complete_phases.add(phase)

        ordered: list[dict[str, Any]] = []
        for phase in _PHASE_ORDER:
            issues = groups.pop(phase, [])
            complete = phase in complete_phases
            deps = phase_deps.get(phase, [])
            locked = _compute_locked(phase, deps, complete_phases)
            ordered.append(
                {
                    "label": phase,
                    "issues": issues,
                    "locked": locked,
                    "complete": complete,
                    "depends_on": deps,
                }
            )

        if not initiative:
            # Legacy: append remaining label buckets, then unphased.
            for label, issues in groups.items():
                complete = bool(issues) and all(i["state"] == "closed" for i in issues)
                ordered.append(
                    {
                        "label": label,
                        "issues": issues,
                        "locked": False,
                        "complete": complete,
                        "depends_on": [],
                    }
                )

        return ordered
    except Exception as exc:
        logger.warning("⚠️  get_issues_grouped_by_phase DB query failed (non-fatal): %s", exc)
        return []


async def get_runs_for_issue_numbers(
    issue_numbers: list[int],
) -> dict[int, dict[str, Any]]:
    """Return the most-recent agent run keyed by issue number.

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
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            if row.issue_number is None or row.issue_number in seen:
                continue
            seen.add(row.issue_number)
            out[row.issue_number] = {
                "id": row.id,
                "role": row.role,
                "status": row.status,
                "pr_number": row.pr_number,
                "branch": row.branch,
                "spawned_at": row.spawned_at.isoformat(),
                "last_activity_at": (
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
            }
        return out
    except Exception as exc:
        logger.warning("⚠️  get_runs_for_issue_numbers DB query failed (non-fatal): %s", exc)
        return {}


async def get_pending_launches() -> list[dict[str, Any]]:
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

        launches: list[dict[str, Any]] = []
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
                {
                    "run_id": row.id,
                    "issue_number": row.issue_number,
                    "role": row.role,
                    "branch": row.branch,
                    "worktree_path": row.worktree_path,
                    "host_worktree_path": host_worktree,
                    "batch_id": row.batch_id,
                    "spawned_at": row.spawned_at.isoformat(),
                }
            )
        logger.warning("🗄️  get_pending_launches: returning %d launch(es)", len(launches))
        return launches
    except Exception as exc:
        logger.warning("❌ get_pending_launches DB query FAILED: %s", exc, exc_info=True)
        return []


async def get_agent_events_tail(
    run_id: str,
    after_id: int = 0,
) -> list[dict[str, Any]]:
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
            {
                "id": row.id,
                "event_type": row.event_type,
                "payload": json.loads(row.payload or "{}"),
                "recorded_at": row.recorded_at.isoformat(),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_events_tail DB query failed (non-fatal): %s", exc)
        return []


async def get_agent_thoughts_tail(
    run_id: str,
    after_seq: int = -1,
    roles: tuple[str, ...] = ("thinking", "assistant"),
) -> list[dict[str, Any]]:
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
            {
                "seq": row.sequence_index,
                "role": row.role,
                "content": row.content or "",
                "recorded_at": row.recorded_at.isoformat(),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  get_agent_thoughts_tail DB query failed (non-fatal): %s", exc)
        return []
