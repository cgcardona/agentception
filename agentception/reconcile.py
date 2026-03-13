from __future__ import annotations

"""Stale-run reconciliation — detect and complete implementing runs that are stuck.

Agent runs that crash, hit the iteration limit, or skip ``build_complete_run``
leave their DB row permanently at ``implementing``.  This module provides
``reconcile_stale_runs()``, which detects those runs and transitions them to
``completed`` based on two GitHub signals:

1. **issue_closed** — the linked GitHub issue is in ``closed`` state.
2. **pr_merged**    — the agent's branch has been merged into ``dev``.

Safety contract
---------------
Only runs whose ``last_activity_at`` is older than ``stale_threshold_minutes``
are considered.  Runs with a recent heartbeat are left untouched — those agents
may still be active.

Each row is committed independently so a GitHub API failure on one candidate
does not roll back mutations already applied to earlier candidates.
"""

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.models import ACAgentRun
from agentception.readers.github import get_issue, is_branch_merged_into

logger = logging.getLogger(__name__)

_UTC = datetime.timezone.utc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


async def reconcile_stale_runs(
    session: AsyncSession,
    *,
    stale_threshold_minutes: int = 10,
) -> list[str]:
    """Detect and complete runs stuck at implementing.

    Queries for ``ACAgentRun`` rows with ``status = 'implementing'`` whose
    ``last_activity_at`` is older than *stale_threshold_minutes*.  For each
    candidate the function checks GitHub in priority order:

    1. If ``issue_number`` is set, calls :func:`get_issue` — marks stale when
       ``state == 'closed'`` (signal ``"issue_closed"``).
    2. If ``branch`` is set, calls :func:`is_branch_merged_into` with
       ``base="dev"`` — marks stale when the branch has been merged (signal
       ``"pr_merged"``).

    Runs that have neither ``issue_number`` nor ``branch`` are skipped entirely
    (no GitHub call, no mutation).

    Parameters
    ----------
    session:
        An open :class:`~sqlalchemy.ext.asyncio.AsyncSession`.  The caller is
        responsible for its lifecycle; this function does **not** close it.
    stale_threshold_minutes:
        Minimum age (in minutes) of ``last_activity_at`` before a run is
        considered a reconciliation candidate.  Defaults to 10 minutes.
        Set to a higher value in production to avoid racing active agents.

    Returns
    -------
    list[str]
        Run IDs that were transitioned to ``completed`` during this call.

    Safety contract
    ---------------
    Runs with ``last_activity_at`` within *stale_threshold_minutes* of now are
    **never** touched.  Each reconciled row is committed independently so a
    GitHub API error on row N does not roll back row N-1.
    """
    cutoff = _utcnow() - datetime.timedelta(minutes=stale_threshold_minutes)

    result = await session.execute(
        select(ACAgentRun).where(
            ACAgentRun.status == "implementing",
            ACAgentRun.last_activity_at < cutoff,
        )
    )
    candidates: list[ACAgentRun] = list(result.scalars().all())

    reconciled: list[str] = []

    for run in candidates:
        # Skip runs with no GitHub signals — nothing to check.
        if run.issue_number is None and run.branch is None:
            continue

        signal: str | None = None

        # Signal 1: linked issue is closed.
        if run.issue_number is not None:
            try:
                issue_data = await get_issue(run.issue_number)
                if issue_data.get("state") == "closed":
                    signal = "issue_closed"
            except Exception as exc:
                logger.warning(
                    "[reconcile] run_id=%s issue_number=%s get_issue failed: %s",
                    run.id,
                    run.issue_number,
                    exc,
                )

        # Signal 2: PR branch has been merged into dev (only if signal 1 not triggered).
        if signal is None and run.branch is not None:
            try:
                merged = await is_branch_merged_into(run.branch, base="dev")
                if merged:
                    signal = "pr_merged"
            except Exception as exc:
                logger.warning(
                    "[reconcile] run_id=%s branch=%r is_branch_merged_into failed: %s",
                    run.id,
                    run.branch,
                    exc,
                )

        if signal is None:
            # Neither signal fired — leave the run alone.
            continue

        # Mutate and commit independently so failures on later rows don't
        # roll back this row.
        run.status = "completed"
        run.last_activity_at = _utcnow()
        try:
            await session.commit()
            logger.info(
                "[reconcile] run_id=%s signal=%s -> completed",
                run.id,
                signal,
            )
            reconciled.append(run.id)
        except Exception as exc:
            logger.warning(
                "[reconcile] run_id=%s commit failed: %s",
                run.id,
                exc,
            )
            await session.rollback()

    return reconciled
