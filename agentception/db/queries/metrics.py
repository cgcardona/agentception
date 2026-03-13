from __future__ import annotations

"""Domain: daily metrics, throughput counts, and merged PR queries."""

import datetime
import json
import logging

from sqlalchemy import func, select

from agentception.db.engine import get_session
from agentception.db.models import ACAgentEvent, ACAgentRun, ACIssue

_COST_INPUT_PER_MTOK: float = 3.00
_COST_OUTPUT_PER_MTOK: float = 15.00
_COST_CACHE_WRITE_PER_MTOK: float = 3.75
_COST_CACHE_READ_PER_MTOK: float = 0.30

from agentception.db.queries.types import (
    StatusCountRow,
    DailyMetrics,
)

logger = logging.getLogger(__name__)

async def get_run_status_counts() -> list[StatusCountRow]:
    """Return total run count per status across all time.

    Used by ``query_dispatcher_state`` and ``query_system_health``.
    Returns ``[]`` on DB error.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun.status, func.count().label("cnt"))
                .group_by(ACAgentRun.status)
                .order_by(ACAgentRun.status)
            )
            rows = result.all()
        return [StatusCountRow(status=str(row.status), count=int(row.cnt)) for row in rows]
    except Exception as exc:
        logger.warning("⚠️  get_run_status_counts DB query failed (non-fatal): %s", exc)
        return []


async def get_daily_metrics(date: datetime.date) -> DailyMetrics:
    """Return a KPI snapshot for *date* derived entirely from DB tables.

    Fields:
    - date: ISO string ("YYYY-MM-DD").
    - issues_closed: ACIssue rows where closed_at falls on *date* (UTC).
    - prs_merged: ACAgentRun rows for *date* with pr_number IS NOT NULL and status == "done".
    - reviewer_runs: ACAgentRun rows for *date* with role == "reviewer".
    - grade_a/b/c/d/f_count: counts by letter extracted from reviewer done-event payloads.
    - first_pass_rate: (grade_a + grade_b) / reviewer_runs; 0.0 when reviewer_runs == 0.
    - rework_rate: developer runs with attempt_number > 0 / total developer runs; 0.0 when 0.
    - avg_iterations: mean step_start event count per completed developer run; 0.0 when none.
    - max_iter_hit_count: completed developer runs with step_start count >= 19.
    - avg_cycle_time_seconds: mean (completed_at - spawned_at).total_seconds() for completed
      developer runs; 0.0 when none.
    - cost_usd: sum across all runs of token-weighted cost using module constants.
    - cost_per_issue_usd: cost_usd / max(issues_closed, 1).
    - redispatch_count: runs (any role) with attempt_number > 0.
    - auto_merge_rate: reviewer runs with grade A or B / reviewer_runs; 0.0 when 0.
    """
    day_start = datetime.datetime.combine(
        date, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    day_end = datetime.datetime.combine(
        date + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc
    )

    try:
        async with get_session() as session:
            # All runs for the day
            runs_result = await session.execute(
                select(ACAgentRun).where(
                    ACAgentRun.spawned_at >= day_start,
                    ACAgentRun.spawned_at < day_end,
                )
            )
            runs: list[ACAgentRun] = list(runs_result.scalars().all())

            # issues_closed
            closed_result = await session.execute(
                select(func.count()).where(
                    ACIssue.closed_at >= day_start,
                    ACIssue.closed_at < day_end,
                )
            )
            issues_closed: int = closed_result.scalar_one() or 0

            # step_start counts per completed developer run
            dev_runs = [
                r for r in runs if r.role == "developer" and r.status == "done"
            ]
            dev_run_ids = [r.id for r in dev_runs]
            step_counts: dict[str, int] = {}
            if dev_run_ids:
                events_result = await session.execute(
                    select(ACAgentEvent.agent_run_id, func.count().label("cnt"))
                    .where(
                        ACAgentEvent.agent_run_id.in_(dev_run_ids),
                        ACAgentEvent.event_type == "step_start",
                    )
                    .group_by(ACAgentEvent.agent_run_id)
                )
                for row in events_result.all():
                    step_counts[row.agent_run_id] = int(row.cnt)

            # reviewer done-event grades
            reviewer_runs = [r for r in runs if r.role == "reviewer"]
            reviewer_run_ids = [r.id for r in reviewer_runs]
            grade_counts: dict[str, int] = {
                "A": 0, "B": 0, "C": 0, "D": 0, "F": 0
            }
            if reviewer_run_ids:
                done_result = await session.execute(
                    select(ACAgentEvent.agent_run_id, ACAgentEvent.payload)
                    .where(
                        ACAgentEvent.agent_run_id.in_(reviewer_run_ids),
                        ACAgentEvent.event_type == "done",
                    )
                )
                for grade_row in done_result.all():
                    try:
                        payload = json.loads(grade_row.payload or "{}")
                        grade = str(payload.get("grade", "")).upper()
                        if grade in grade_counts:
                            grade_counts[grade] += 1
                    except Exception:
                        pass

    except Exception as exc:
        logger.warning("⚠️  get_daily_metrics DB query failed: %s", exc)
        return DailyMetrics(
            date=date.isoformat(),
            issues_closed=0, prs_merged=0, reviewer_runs=0,
            grade_a_count=0, grade_b_count=0, grade_c_count=0,
            grade_d_count=0, grade_f_count=0,
            first_pass_rate=0.0, rework_rate=0.0, avg_iterations=0.0,
            max_iter_hit_count=0, avg_cycle_time_seconds=0.0,
            cost_usd=0.0, cost_per_issue_usd=0.0,
            redispatch_count=0, auto_merge_rate=0.0,
        )

    # --- derived metrics ---
    prs_merged = sum(
        1 for r in runs if r.pr_number is not None and r.status == "done"
    )
    reviewer_run_count = len(reviewer_runs)

    all_dev_runs = [r for r in runs if r.role == "developer"]
    rework_dev = sum(1 for r in all_dev_runs if r.attempt_number > 0)
    rework_rate = rework_dev / max(len(all_dev_runs), 1) if all_dev_runs else 0.0

    iter_counts = [step_counts.get(r.id, 0) for r in dev_runs]
    avg_iterations = sum(iter_counts) / max(len(iter_counts), 1) if iter_counts else 0.0
    max_iter_hit_count = sum(1 for c in iter_counts if c >= 19)

    cycle_times = [
        (r.completed_at - r.spawned_at).total_seconds()
        for r in dev_runs
        if r.completed_at is not None
    ]
    avg_cycle_time_seconds = sum(cycle_times) / max(len(cycle_times), 1) if cycle_times else 0.0

    cost_usd = sum(
        (r.total_input_tokens / 1_000_000) * _COST_INPUT_PER_MTOK
        + (r.total_output_tokens / 1_000_000) * _COST_OUTPUT_PER_MTOK
        + (r.total_cache_write_tokens / 1_000_000) * _COST_CACHE_WRITE_PER_MTOK
        + (r.total_cache_read_tokens / 1_000_000) * _COST_CACHE_READ_PER_MTOK
        for r in runs
    )
    cost_per_issue_usd = cost_usd / max(issues_closed, 1)

    redispatch_count = sum(1 for r in runs if r.attempt_number > 0)

    first_pass_rate = (
        (grade_counts["A"] + grade_counts["B"]) / reviewer_run_count
        if reviewer_run_count > 0 else 0.0
    )
    auto_merge_rate = (
        (grade_counts["A"] + grade_counts["B"]) / reviewer_run_count
        if reviewer_run_count > 0 else 0.0
    )

    return DailyMetrics(
        date=date.isoformat(),
        issues_closed=issues_closed,
        prs_merged=prs_merged,
        reviewer_runs=reviewer_run_count,
        grade_a_count=grade_counts["A"],
        grade_b_count=grade_counts["B"],
        grade_c_count=grade_counts["C"],
        grade_d_count=grade_counts["D"],
        grade_f_count=grade_counts["F"],
        first_pass_rate=first_pass_rate,
        rework_rate=rework_rate,
        avg_iterations=avg_iterations,
        max_iter_hit_count=max_iter_hit_count,
        avg_cycle_time_seconds=avg_cycle_time_seconds,
        cost_usd=cost_usd,
        cost_per_issue_usd=cost_per_issue_usd,
        redispatch_count=redispatch_count,
        auto_merge_rate=auto_merge_rate,
    )

