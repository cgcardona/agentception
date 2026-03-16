from __future__ import annotations

"""Regression test for the reviewer orphan-sweep guard in db/persist.py.

Bug: reviewer runs have pr_number set AT DISPATCH TIME (the PR already
exists before the reviewer starts).  The orphan sweep's heuristic
  "implementing + pr_number + not in live_ids → completed"
was designed for executor runs where pr_number is written only after the
executor finishes and opens the PR.  Applying it to a reviewer would
kill the run immediately after creation — before the first LLM call.

Fix: _upsert_agent_runs() now skips the orphan sweep for runs whose
role is "reviewer".  Reviewer lifecycle is always driven by
build_complete_run, never by poller inference.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_persist_reviewer_orphan_guard.py -v
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.models import ACAgentRun
from agentception.models import AgentNode, AgentStatus

_UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reviewer_run(role: str = "reviewer") -> ACAgentRun:
    """Return a minimal ACAgentRun in implementing state with a pr_number set."""
    return ACAgentRun(
        id="review-576",
        role=role,
        status="implementing",
        issue_number=575,
        pr_number=576,
        branch="agent/issue-575",
        worktree_path="/worktrees/review-576",
        spawned_at=datetime.datetime.now(_UTC),
    )


def _make_session(existing_run: ACAgentRun) -> MagicMock:
    """Return a mock AsyncSession wired for _upsert_agent_runs.

    _upsert_agent_runs performs three sequential execute() calls:
      1. Per-agent row lookup (scalar_one_or_none)
      2. Orphan sweep (scalars().all())
      3. Pending-launch TTL sweep (scalars().all())
    """
    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = existing_run

    orphan_result = MagicMock()
    orphan_result.scalars.return_value.all.return_value = [existing_run]

    ttl_result = MagicMock()
    ttl_result.scalars.return_value.all.return_value = []

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[lookup, orphan_result, ttl_result])
    session.add = MagicMock()
    return session


def _make_session_no_agents(existing_run: ACAgentRun) -> MagicMock:
    """Session for the empty-agents-list case (no per-agent lookup)."""
    orphan_result = MagicMock()
    orphan_result.scalars.return_value.all.return_value = [existing_run]

    ttl_result = MagicMock()
    ttl_result.scalars.return_value.all.return_value = []

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[orphan_result, ttl_result])
    # scalar() is called once per orphan to count build_complete_run events.
    # Return 0 — no build_complete_run event present.
    session.scalar = AsyncMock(return_value=0)
    session.add = MagicMock()
    return session


def _make_agent_node() -> AgentNode:
    return AgentNode(
        id="review-576",
        role="reviewer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=575,
        pr_number=576,
        branch="agent/issue-575",
        worktree_path="/worktrees/review-576",
        cognitive_arch="michael_fagan:python",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reviewer_not_orphaned_when_missing_from_live_ids() -> None:
    """Orphan sweep must NOT mark a reviewer run as completed.

    Scenario: the poller runs a tick where the reviewer is not in the
    agents list (live_ids is empty).  Before the fix the sweep would see
    role=reviewer + pr_number + not_in_live_ids and set status="completed",
    killing the run before its first LLM iteration.
    """
    from agentception.db.persist import _upsert_agent_runs  # noqa: PLC0415

    reviewer_run = _make_reviewer_run(role="reviewer")
    session = _make_session_no_agents(reviewer_run)

    await _upsert_agent_runs(session, agents=[])

    assert reviewer_run.status == "implementing", (
        f"Reviewer run was orphaned: status={reviewer_run.status!r}. "
        "The orphan sweep must skip reviewer runs regardless of pr_number."
    )


@pytest.mark.anyio
async def test_executor_still_orphaned_when_missing_from_live_ids() -> None:
    """Orphan sweep must still mark a non-reviewer run as failed when no build_complete_run event.

    The reviewer exclusion must not accidentally protect executor/developer
    runs — their orphan → failed transition must still work when there is no
    build_complete_run event (even if pr_number is set).
    """
    from agentception.db.persist import _upsert_agent_runs  # noqa: PLC0415

    executor_run = _make_reviewer_run(role="developer")
    session = _make_session_no_agents(executor_run)

    await _upsert_agent_runs(session, agents=[])

    assert executor_run.status == "failed", (
        f"Executor run was NOT orphaned: status={executor_run.status!r}. "
        "Orphan sweep must mark non-reviewer runs as failed when no build_complete_run event."
    )


@pytest.mark.anyio
async def test_reviewer_in_live_ids_not_completed() -> None:
    """When the reviewer IS in live_ids its status must not become completed."""
    from agentception.db.persist import _upsert_agent_runs  # noqa: PLC0415

    reviewer_run = _make_reviewer_run(role="reviewer")
    node = _make_agent_node()
    session = _make_session(reviewer_run)

    await _upsert_agent_runs(session, agents=[node])

    assert reviewer_run.status != "completed", (
        "Reviewer run must not be completed when present in live_ids."
    )
