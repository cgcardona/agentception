from __future__ import annotations

"""Domain: agent run lifecycle and state queries."""

import datetime
import json
import logging
import re as _re
from pathlib import Path

from sqlalchemy import select, text

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentEvent,
    ACAgentMessage,
    ACAgentRun,
    ACExecutionPlan,
)
from agentception.workflow.status import (
    LIVE_STATUSES as _LIVE_STATUSES,
    STALE_THRESHOLD,
)

_ACTIVE_STATUSES = _LIVE_STATUSES
_STALE_THRESHOLD_SECONDS: int = int(STALE_THRESHOLD.total_seconds())
_AC_ISSUE_BRANCH_RE: _re.Pattern[str] = _re.compile(r"^ac/issue-(\d+)")

from agentception.db.queries.types import (
    AgentRunRow,
    AgentMessageRow,
    AgentRunDetail,
    SiblingRunRow,
    AgentRunTeardownRow,
    RunForIssueRow,
    RunTreeNodeRow,
    _RunStepData,
    PendingLaunchRow,
    TerminalRunRow,
    RunSummaryRow,
    RunContextRow,
)

logger = logging.getLogger(__name__)

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


async def get_agent_run_role(run_id: str) -> str | None:
    """Return the role of a single agent run, or None if not found / on error.

    Intentionally lightweight — fetches only the role column.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.role).where(ACAgentRun.id == run_id)
            )
            row = result.one_or_none()
        return row[0] if row is not None else None
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_role DB query failed (non-fatal): %s", exc)
        return None


async def get_agent_run_task_description(run_id: str) -> str | None:
    """Return the task_description for a single agent run, or None if not found.

    Used by auto_redispatch to count prior reviewer-rejection sections injected
    into the task description — the rejection count determines the attempt number
    without requiring an extra DB column.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.task_description).where(ACAgentRun.id == run_id)
            )
            row = result.one_or_none()
        return row[0] if row is not None else None
    except Exception as exc:
        logger.warning("⚠️  get_agent_run_task_description DB query failed (non-fatal): %s", exc)
        return None


async def get_terminal_runs_with_worktrees() -> list[TerminalRunRow]:
    """Return terminal runs whose worktree directory still exists on disk.

    Terminal statuses are ``completed``, ``failed``, ``cancelled``, and
    ``stopped`` — all mean the agent has finished and the worktree should have
    been cleaned up.  This query powers the worktree reaper that runs on
    startup and periodically so that orphaned worktrees from crashed agents are
    eventually removed without any agent cooperation.

    Only rows with a non-null ``worktree_path`` are returned; the reaper
    filters further to paths that actually exist on disk before calling
    ``release_worktree``.

    **Important:** worktree paths that are currently held by an *active* run
    are excluded.  Without this guard, re-dispatching the same issue creates a
    new run at the same path (e.g. ``/worktrees/issue-276``), but the old
    terminal row still references that path.  The reaper would then find the
    directory, match it to the terminal row, and delete it — killing the live
    agent's workspace.

    Returns an empty list on any DB error so the reaper degrades gracefully.
    """
    # Subquery: worktree paths currently held by a live (non-terminal) run.
    active_paths_sq = (
        select(ACAgentRun.worktree_path)
        .where(
            ACAgentRun.status.in_(list(_LIVE_STATUSES)),
            ACAgentRun.worktree_path.isnot(None),
        )
        .scalar_subquery()
    )
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.id, ACAgentRun.worktree_path, ACAgentRun.branch).where(
                    ACAgentRun.status.in_(["completed", "failed", "cancelled", "stopped"]),
                    ACAgentRun.worktree_path.isnot(None),
                    ACAgentRun.worktree_path.not_in(active_paths_sq),
                )
            )
            rows = result.all()
        return [
            TerminalRunRow(
                id=row.id,
                worktree_path=row.worktree_path,
                branch=row.branch,
            )
            for row in rows
            if row.worktree_path is not None
        ]
    except Exception as exc:
        logger.warning(
            "⚠️  get_terminal_runs_with_worktrees DB query failed (non-fatal): %s", exc
        )
        return []


def _run_to_summary(row: ACAgentRun) -> RunSummaryRow:
    """Convert an ACAgentRun ORM row to a RunSummaryRow."""
    return RunSummaryRow(
        run_id=row.id,
        status=row.status,
        role=row.role,
        issue_number=row.issue_number,
        pr_number=row.pr_number,
        branch=row.branch,
        worktree_path=row.worktree_path,
        batch_id=row.batch_id,
        tier=row.tier,
        org_domain=row.org_domain,
        parent_run_id=row.parent_run_id,
        spawned_at=row.spawned_at.isoformat(),
        last_activity_at=(row.last_activity_at.isoformat() if row.last_activity_at else None),
        completed_at=(row.completed_at.isoformat() if row.completed_at else None),
    )


async def get_run_by_id(run_id: str) -> RunSummaryRow | None:
    """Return lightweight run metadata for a single run.

    Agents call this on startup to determine their current state and decide
    whether to resume, block, or complete.  Returns ``None`` if the run does
    not exist or on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return _run_to_summary(row)
    except Exception as exc:
        logger.warning("⚠️  get_run_by_id DB query failed (non-fatal): %s", exc)
        return None


async def get_run_context(run_id: str) -> RunContextRow | None:
    """Return the full task context for *run_id* from the DB.

    Unlike :func:`get_run_by_id`, this includes ``cognitive_arch`` and
    ``task_description`` — everything needed to fully brief an agent or
    populate the ``ac://runs/{run_id}/context`` MCP resource.

    Returns ``None`` when the run does not exist or on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return RunContextRow(
            run_id=row.id,
            status=row.status,
            role=row.role,
            cognitive_arch=row.cognitive_arch,
            task_description=row.task_description,
            issue_number=row.issue_number,
            pr_number=row.pr_number,
            branch=row.branch,
            worktree_path=row.worktree_path,
            batch_id=row.batch_id,
            tier=row.tier,
            org_domain=row.org_domain,
            parent_run_id=row.parent_run_id,
            gh_repo=row.gh_repo,
            is_resumed=row.is_resumed,
            coord_fingerprint=row.coord_fingerprint,
            spawned_at=row.spawned_at.isoformat(),
            last_activity_at=(row.last_activity_at.isoformat() if row.last_activity_at else None),
            completed_at=(row.completed_at.isoformat() if row.completed_at else None),
        )
    except Exception as exc:
        logger.warning("⚠️  get_run_context DB query failed (non-fatal): %s", exc)
        return None


async def list_active_runs() -> list[RunContextRow]:
    """Return all agent runs currently in an active state (DB-only replacement for worktree scan).

    Returns rows with status in ``{implementing, pending_launch, reviewing}``.
    Used by the poller to build the board and detect alerts — replaces the
    old ``list_active_worktrees()`` filesystem scan.  Returns ``[]`` on error.
    """
    _ACTIVE_STATUSES = {"implementing", "pending_launch", "reviewing"}
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.status.in_(_ACTIVE_STATUSES))
            )
            rows = result.scalars().all()
        return [
            RunContextRow(
                run_id=row.id,
                status=row.status,
                role=row.role,
                cognitive_arch=row.cognitive_arch,
                task_description=row.task_description,
                issue_number=row.issue_number,
                pr_number=row.pr_number,
                branch=row.branch,
                worktree_path=row.worktree_path,
                batch_id=row.batch_id,
                tier=row.tier,
                org_domain=row.org_domain,
                parent_run_id=row.parent_run_id,
                gh_repo=row.gh_repo,
                is_resumed=row.is_resumed,
                coord_fingerprint=row.coord_fingerprint,
                spawned_at=row.spawned_at.isoformat(),
                last_activity_at=(row.last_activity_at.isoformat() if row.last_activity_at else None),
                completed_at=(row.completed_at.isoformat() if row.completed_at else None),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  list_active_runs DB query failed (non-fatal): %s", exc)
        return []


async def get_children_by_parent_id(parent_run_id: str) -> list[RunSummaryRow]:
    """Return all runs spawned by *parent_run_id*, ordered by spawn time.

    Used by ``query_children`` MCP tool.  Returns ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.parent_run_id == parent_run_id)
                .order_by(ACAgentRun.spawned_at.asc())
            )
            rows = result.scalars().all()
        return [_run_to_summary(r) for r in rows]
    except Exception as exc:
        logger.warning("⚠️  get_children_by_parent_id DB query failed (non-fatal): %s", exc)
        return []


async def get_active_runs() -> list[RunSummaryRow]:
    """Return all runs currently in a live or blocked state.

    Live statuses: ``pending_launch``, ``implementing``, ``reviewing``,
    ``blocked``.  Used by ``query_active_runs`` MCP tool.

    Returns ``[]`` on DB error.
    """
    active_statuses = ["pending_launch", "implementing", "reviewing", "blocked"]
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun)
                .where(ACAgentRun.status.in_(active_statuses))
                .order_by(ACAgentRun.spawned_at.desc())
            )
            rows = result.scalars().all()
        return [_run_to_summary(r) for r in rows]
    except Exception as exc:
        logger.warning("⚠️  get_active_runs DB query failed (non-fatal): %s", exc)
        return []


async def check_db_reachable() -> bool:
    """Return True if the DB responds to a trivial query, False otherwise."""
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def get_run_by_worktree_path(worktree_path: str) -> RunSummaryRow | None:
    """Return lightweight run metadata for the run whose worktree lives at *worktree_path*.

    Used by the kill endpoint to resolve the issue number for ``agent/wip``
    label cleanup from the DB row.  Returns ``None``
    when no run matches the path or on DB error.

    Args:
        worktree_path: Absolute container-side path of the worktree directory
                       (e.g. ``/worktrees/issue-553``).

    Returns:
        :class:`RunSummaryRow` on success, ``None`` otherwise.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.worktree_path == worktree_path)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return _run_to_summary(row)
    except Exception as exc:
        logger.warning(
            "⚠️  get_run_by_worktree_path DB query failed (non-fatal): %s", exc
        )
        return None


async def load_execution_plan(run_id: str) -> str | None:
    """Return the raw ``plan_json`` for *run_id*, or ``None`` if absent.

    Args:
        run_id: Agent run identifier (e.g. ``"issue-501"``).

    Returns:
        The serialised ``ExecutionPlan`` JSON string, or ``None`` when no
        plan exists for this run or on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACExecutionPlan).where(ACExecutionPlan.run_id == run_id)
            )
            row = result.scalar_one_or_none()
        return row.plan_json if row is not None else None
    except Exception as exc:
        logger.warning(
            "⚠️  load_execution_plan DB query failed (non-fatal): %s", exc
        )
        return None

