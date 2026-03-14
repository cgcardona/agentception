from __future__ import annotations

"""Domain: board issues, initiative phases, and label state queries."""

import json
import logging
import re as _re
from pathlib import Path

from sqlalchemy import func, select, text

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentRun,
    ACIssue,
    ACIssueWorkflowState,
    ACPipelineSnapshot,
    ACPullRequest,
    ACWave,
)
_AC_ISSUE_BRANCH_RE: _re.Pattern[str] = _re.compile(r"^ac/issue-(\d+)")
_MD_STRIP_RE = _re.compile(r"[*`_\[\]!>|~]+|```[^`]*```")
_MD_SPACES_RE = _re.compile(r" +")

from agentception.db.queries.types import (
    LabelEntry,
    BoardIssueRow,
    PipelineTrendRow,
    AgentRunRow,
    OpenPRRow,
    LinkedPRRow,
    IssueAgentRunRow,
    IssueDetailRow,
    AllIssueRow,
    LinkedIssueRow,
    PRAgentRunRow,
    PRDetailRow,
    AllPRRow,
    ShipReviewerRunRow,
    ShipPRRow,
    ShipPhaseGroupRow,
    WaveAgentRow,
    WaveRow,
    ConductorHistoryRow,
    PhasedIssueRow,
    PhaseGroupRow,
    OpenPRForIssueRow,
    WorkflowStateRow,
    InitiativePhaseMeta,
    PhaseSummary,
    IssueSummary,
    LabelContext,
    BlockedDepsRow,
    InitiativeIssueRow,
    InitiativePhaseRow,
    InitiativeSummary,
)

logger = logging.getLogger(__name__)

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
            is_claimed = "agent/wip" in labels
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
            1 for r in rows if "agent/wip" in json.loads(r.labels_json)
        )
        return {"total": total, "claimed": claimed, "unclaimed": total - claimed}
    except Exception as exc:
        logger.warning("⚠️  get_board_counts DB query failed (non-fatal): %s", exc)
        return {"total": 0, "claimed": 0, "unclaimed": 0}


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
            claimed="agent/wip" in labels,
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


async def get_waves_from_db(limit: int = 100) -> list[WaveRow]:
    """Return agent runs grouped by batch_id as wave-shaped dicts.

    Used by ``telemetry.aggregate_waves()`` when no worktrees exist
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


async def get_conductor_history(
    limit: int = 5,
    worktrees_dir: Path | None = None,
    host_worktrees_dir: Path | None = None,
) -> list[ConductorHistoryRow]:
    """Return the last *limit* conductor spawns with current active/completed status.

    Status is derived from the latest agent run in each wave: ``"active"`` when
    the run status is ``implementing`` or ``reviewing``, ``"completed"`` otherwise.
    Falls back to ``[]`` on any DB error so the UI degrades gracefully without
    surfacing the error to the user.
    """
    from sqlalchemy import desc, func

    from agentception.config import settings

    wt_dir = worktrees_dir or settings.worktrees_dir
    host_wt_dir = host_worktrees_dir or settings.host_worktrees_dir

    try:
        async with get_session() as session:
            # Subquery: pick the most-recent agent run per wave (by spawned_at).
            latest_run_subq = (
                select(
                    ACAgentRun.wave_id,
                    ACAgentRun.status,
                    func.row_number()
                    .over(
                        partition_by=ACAgentRun.wave_id,
                        order_by=desc(ACAgentRun.spawned_at),
                    )
                    .label("rn"),
                )
                .where(ACAgentRun.wave_id.isnot(None))
                .subquery()
            )

            stmt = (
                select(ACWave, latest_run_subq.c.status)
                .outerjoin(
                    latest_run_subq,
                    (ACWave.id == latest_run_subq.c.wave_id)
                    & (latest_run_subq.c.rn == 1),
                )
                .where(ACWave.role == "conductor")
                .order_by(desc(ACWave.started_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.all()

        entries: list[ConductorHistoryRow] = []
        for wave, run_status in rows:
            worktree = Path(wt_dir) / wave.id
            host_worktree = Path(host_wt_dir) / wave.id
            # Replaced filesystem worktree check — status is the authoritative signal.
            display_status = (
                "active" if run_status in ("implementing", "reviewing") else "completed"
            )
            entries.append(
                ConductorHistoryRow(
                    wave_id=wave.id,
                    worktree=str(worktree),
                    host_worktree=str(host_worktree),
                    started_at=wave.started_at.strftime("%Y-%m-%d %H:%M UTC"),
                    status=display_status,
                )
            )
        return entries
    except Exception as exc:
        logger.warning("⚠️  get_conductor_history DB query failed (non-fatal): %s", exc)
        return []


async def get_initiative_phase_meta(
    repo: str,
    initiative: str,
) -> list[InitiativePhaseMeta]:
    """Return ordered phase metadata for the latest batch of an initiative.

    Finds the most recent ``batch_id`` for ``(repo, initiative)`` (by
    ``created_at DESC``) then returns all phases in that batch sorted by
    ``phase_order ASC``.

    Returns ``[]`` when no rows exist (initiative not yet filed or predates
    this feature); callers fall back to lexicographic sort in that case.
    Falls back to ``[]`` on DB error.
    """
    from agentception.db.models import ACInitiativePhase
    from sqlalchemy import func

    try:
        async with get_session() as session:
            # Find the latest batch_id for this repo+initiative.
            latest_batch_result = await session.execute(
                select(ACInitiativePhase.batch_id)
                .where(
                    ACInitiativePhase.repo == repo,
                    ACInitiativePhase.initiative == initiative,
                )
                .order_by(func.min(ACInitiativePhase.created_at).desc())
                .group_by(ACInitiativePhase.batch_id)
                .limit(1)
            )
            latest_batch_id: str | None = latest_batch_result.scalar_one_or_none()
            if latest_batch_id is None:
                return []

            result = await session.execute(
                select(ACInitiativePhase)
                .where(
                    ACInitiativePhase.repo == repo,
                    ACInitiativePhase.initiative == initiative,
                    ACInitiativePhase.batch_id == latest_batch_id,
                )
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


async def get_initiatives(repo: str) -> list[str]:
    """Return initiative slugs filed via Plan 1B that still have open issues.

    Derives the tab list exclusively from ``initiative_phases`` — if an
    initiative was filed through the Plan pipeline it appears here; no JSON
    configuration is required.

    Ordering: most recently filed batch first (``MAX(created_at) DESC``), then
    alphabetically as a tiebreaker so the result is stable.

    An initiative drops out automatically once all its GitHub issues are closed,
    keeping the tab bar noise-free without manual maintenance.

    Falls back to ``[]`` on DB error (non-fatal degradation).
    """
    from agentception.db.models import ACInitiativePhase

    try:
        # Step 1: ordered list of initiatives from the filing history.
        async with get_session() as session:
            phase_result = await session.execute(
                select(
                    ACInitiativePhase.initiative,
                    func.max(ACInitiativePhase.created_at).label("last_filed"),
                )
                .where(ACInitiativePhase.repo == repo)
                .group_by(ACInitiativePhase.initiative)
                .order_by(
                    func.max(ACInitiativePhase.created_at).desc(),
                    ACInitiativePhase.initiative,
                )
            )
            ordered: list[str] = [row.initiative for row in phase_result.all()]

        if not ordered:
            return []

        # Step 2: which of those still have at least one open issue with a
        # scoped phase label (e.g. "auth-rewrite/0-foundation")?
        # An initiative whose issues all closed is hidden automatically.
        async with get_session() as session:
            issue_result = await session.execute(
                select(ACIssue.labels_json)
                .where(ACIssue.repo == repo, ACIssue.state == "open")
            )
            open_labels: list[list[str]] = [
                json.loads(row[0] or "[]") for row in issue_result.all()
            ]

        has_open: set[str] = set()
        for ini in ordered:
            prefix = f"{ini}/"
            if any(any(lbl.startswith(prefix) for lbl in lbls) for lbls in open_labels):
                has_open.add(ini)

        return [ini for ini in ordered if ini in has_open]

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


def _body_excerpt(body: str | None, max_chars: int = 120) -> str:
    """Return a plain-text excerpt from the first prose paragraph of an issue body.

    Skips markdown section headers (lines beginning with ``#``) and leading
    blank lines so the card subtitle shows actual description text rather than
    repeating the section label (e.g. "Context", "Objective").  Stops at the
    first blank line after content begins, giving one clean prose paragraph.

    Remaining inline markdown (bold, code, etc.) is stripped by ``_MD_STRIP_RE``.
    """
    if not isinstance(body, str) or not body:
        return ""

    # Collect lines from the first prose paragraph, skipping header lines.
    content_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # A new header after content has started ends the first paragraph.
            if content_lines:
                break
            continue
        if not stripped:
            # Blank line ends the first paragraph once content has started;
            # blank lines before any content are ignored.
            if content_lines:
                break
            continue
        content_lines.append(stripped)

    if not content_lines:
        return ""

    text = _MD_SPACES_RE.sub(" ", _MD_STRIP_RE.sub(" ", " ".join(content_lines))).strip()
    if len(text) <= max_chars:
        return text
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

        # Build a state map so dep chips are only shown for open dependencies.
        # Closed deps are no longer blockers — they should not appear on cards.
        issue_state_map: dict[int, str] = {r.github_number: r.state for r in rows}

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

            all_deps: list[int] = json.loads(row.depends_on_json or "[]")
            # Only surface deps that are still open — a closed dep is resolved
            # and should not dim the card or confuse agents about eligibility.
            open_deps: list[int] = [
                dep for dep in all_deps
                if issue_state_map.get(dep, "open") != "closed"
            ]
            groups.setdefault(phase_key, []).append(
                PhasedIssueRow(
                    number=row.github_number,
                    title=row.title,
                    body_excerpt=_body_excerpt(row.body),
                    state=row.state,
                    url=f"https://github.com/{repo}/issues/{row.github_number}",
                    labels=issue_labels,
                    depends_on=open_deps,
                )
            )

        # Determine effective display order and load dep graph.
        effective_phase_order: list[str]
        phase_deps: dict[str, list[str]] = {}

        if phase_order is not None:
            # Caller supplied an explicit ordering — use it as-is.
            effective_phase_order = phase_order
        elif initiative:
            meta = await get_initiative_phase_meta(repo, initiative)
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


async def get_blocked_deps_open_issues(repo: str) -> list[BlockedDepsRow]:
    """Return open issues that still carry the ``blocked/deps`` label.

    Used by the poller to decide which ``blocked/deps`` labels can be
    removed because all ticket-level dependencies have since closed.

    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue).where(
                    ACIssue.repo == repo,
                    ACIssue.state == "open",
                )
            )
            rows = result.scalars().all()

        out: list[BlockedDepsRow] = []
        for row in rows:
            labels: list[str] = json.loads(row.labels_json or "[]")
            if "blocked/deps" not in labels:
                continue
            dep_numbers: list[int] = json.loads(row.depends_on_json or "[]")
            if not dep_numbers:
                continue
            out.append(
                BlockedDepsRow(
                    github_number=row.github_number,
                    dep_numbers=dep_numbers,
                )
            )
        return out
    except Exception as exc:
        logger.warning("❌ get_blocked_deps_open_issues failed: %s", exc)
        return []


async def get_issues_missing_blocked_deps(repo: str) -> list[BlockedDepsRow]:
    """Return open issues that have deps recorded but are missing the ``blocked/deps`` label.

    Used by the poller's ``_stamp_missing_blocked_deps`` to re-apply the label
    when the initial stamp in ``file_issues`` failed silently (e.g. a transient
    GitHub API error caught by the old shared try/except).  Only issues whose
    ``depends_on_json`` is non-empty are considered — the body-based backfill in
    ``_upsert_issues`` ensures this field is populated on the first poller tick
    after issue creation.

    Falls back to ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue).where(
                    ACIssue.repo == repo,
                    ACIssue.state == "open",
                )
            )
            rows = result.scalars().all()

        out: list[BlockedDepsRow] = []
        for row in rows:
            dep_numbers: list[int] = json.loads(row.depends_on_json or "[]")
            if not dep_numbers:
                continue
            labels: list[str] = json.loads(row.labels_json or "[]")
            if "blocked/deps" in labels:
                continue
            out.append(
                BlockedDepsRow(
                    github_number=row.github_number,
                    dep_numbers=dep_numbers,
                )
            )
        return out
    except Exception as exc:
        logger.warning("❌ get_issues_missing_blocked_deps failed: %s", exc)
        return []


async def get_closed_issue_numbers(repo: str) -> set[int]:
    """Return the set of GitHub issue numbers that are closed in the DB.

    Used by the poller to check whether ticket-level dependencies have closed.
    Falls back to ``set()`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACIssue.github_number).where(
                    ACIssue.repo == repo,
                    ACIssue.state == "closed",
                )
            )
            return {row for (row,) in result.all()}
    except Exception as exc:
        logger.warning("❌ get_closed_issue_numbers failed: %s", exc)
        return set()


async def get_prs_grouped_by_phase(
    repo: str,
    initiative: str | None = None,
    batch_id: str | None = None,
    limit: int = 200,
) -> list[ShipPhaseGroupRow]:
    """Return PRs grouped by phase label for the Ship board.

    Each PR is matched to its closing issue (via ``closes_issue_number``) to
    determine the phase and initiative membership.  Each PR is enriched with
    the latest ``reviewer`` agent run.

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
                .where(ACAgentRun.role == "reviewer")
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


async def get_initiative_batches(repo: str, initiative: str) -> list[str]:
    """Return batch_ids for a (repo, initiative) pair, newest first.

    Used by ``GET /plan/{org}/{repo}/{initiative}`` to redirect to the most
    recent batch.  Returns ``[]`` when no batches exist or on DB error.
    """
    from agentception.db.models import ACInitiativePhase
    from sqlalchemy import func

    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    ACInitiativePhase.batch_id,
                    func.min(ACInitiativePhase.created_at).label("filed_at"),
                )
                .where(
                    ACInitiativePhase.repo == repo,
                    ACInitiativePhase.initiative == initiative,
                )
                .group_by(ACInitiativePhase.batch_id)
                .order_by(func.min(ACInitiativePhase.created_at).desc())
            )
            return [row.batch_id for row in result.all()]
    except Exception as exc:
        logger.warning("⚠️  get_initiative_batches failed (non-fatal): %s", exc)
        return []


async def get_initiative_summary(
    repo: str,
    initiative: str,
    batch_id: str,
) -> InitiativeSummary | None:
    """Return a complete summary of one filing batch for the shareable plan page.

    Returns ``None`` when no ``initiative_phases`` rows exist for the given
    ``(repo, initiative, batch_id)`` triple (i.e. the batch has never been
    filed via Phase 1B, or the IDs are wrong).  Falls back to ``None`` on DB
    error so the route can return a 404 gracefully.

    Phase ordering follows the canonical ``phase_order`` stored by
    ``persist_initiative_phases``.  Active/complete/locked state is derived
    from ``get_issues_grouped_by_phase`` which handles the dep-graph logic.
    """
    from agentception.db.models import ACInitiativePhase

    try:
        async with get_session() as session:
            phase_result = await session.execute(
                select(ACInitiativePhase)
                .where(
                    ACInitiativePhase.repo == repo,
                    ACInitiativePhase.initiative == initiative,
                    ACInitiativePhase.batch_id == batch_id,
                )
                .order_by(ACInitiativePhase.phase_order)
            )
            phase_rows = phase_result.scalars().all()

        if not phase_rows:
            return None

        filed_at: str | None = min(r.created_at for r in phase_rows).isoformat()

        # Re-use the existing query to get issues grouped by phase with
        # locked/complete state already computed from the dep graph.
        phase_groups = await get_issues_grouped_by_phase(repo, initiative)
        group_by_label: dict[str, PhaseGroupRow] = {g["label"]: g for g in phase_groups}

        phases_out: list[InitiativePhaseRow] = []
        total_issues = 0
        open_count = 0
        closed_count = 0

        for row in phase_rows:
            label = row.phase_label  # scoped, e.g. "auth-rewrite/0-foundation"
            short_label = label.split("/", 1)[1] if "/" in label else label

            _empty: PhaseGroupRow = PhaseGroupRow(
                label=label, issues=[], locked=False, complete=False, depends_on=[]
            )
            group = group_by_label.get(label, _empty)

            is_complete = group["complete"]
            is_active = not group["locked"] and not is_complete

            issue_rows: list[InitiativeIssueRow] = [
                InitiativeIssueRow(
                    number=iss["number"],
                    title=iss["title"],
                    url=iss["url"],
                    state=iss["state"],
                )
                for iss in group["issues"]
            ]

            total_issues += len(issue_rows)
            open_count += sum(1 for i in issue_rows if i["state"] == "open")
            closed_count += sum(1 for i in issue_rows if i["state"] == "closed")

            phases_out.append(InitiativePhaseRow(
                label=label,
                short_label=short_label,
                order=row.phase_order,
                is_active=is_active,
                is_complete=is_complete,
                issues=issue_rows,
            ))

        return InitiativeSummary(
            repo=repo,
            initiative=initiative,
            batch_id=batch_id,
            phase_count=len(phases_out),
            issue_count=total_issues,
            open_count=open_count,
            closed_count=closed_count,
            filed_at=filed_at,
            phases=phases_out,
        )
    except Exception as exc:
        logger.warning("⚠️  get_initiative_summary DB query failed (non-fatal): %s", exc)
        return None

