from __future__ import annotations

"""Regression tests for the pending_launch guard in db/persist.py.

Bug: the poller called _upsert_agent_runs() whenever it found a worktree on
the filesystem.  If that worktree belonged to a pending_launch run (Dispatcher
not yet invoked), the upsert would overwrite the precious pending_launch status
with whatever status the poller derived (typically "stale"), draining the
Dispatcher queue before it was ever read.

Fix: _upsert_agent_runs() now skips the status overwrite when existing.status
is "pending_launch".  Only the /api/build/acknowledge/{run_id} endpoint may
transition out of pending_launch.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_persist_pending_launch_guard.py -v
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.models import ACAgentRun
from agentception.models import AgentNode, AgentStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(status: str = "pending_launch") -> ACAgentRun:
    """Return a minimal ACAgentRun ORM object with the given status."""
    run = ACAgentRun(
        id="label-developer-experience-layer-5492de",
        role="cto",
        status=status,
        branch="agent/developer-experience-layer-b735",
        worktree_path="/worktrees/label-developer-experience-layer-5492de",
        spawned_at=datetime.datetime.now(datetime.timezone.utc),
    )
    return run


def _make_agent(status: AgentStatus = AgentStatus.STALE) -> AgentNode:
    """Return a minimal AgentNode as the poller would produce."""
    return AgentNode(
        id="label-developer-experience-layer-5492de",
        role="cto",
        status=status,
        worktree_path="/worktrees/label-developer-experience-layer-5492de",
    )


def _make_session(existing_run: ACAgentRun | None) -> MagicMock:
    """Return a mock AsyncSession whose execute() yields *existing_run*.

    Uses ``spec=AsyncSession`` so that ``isinstance(session, AsyncSession)``
    passes the guard inside ``_upsert_agent_runs``.
    """
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = existing_run

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []  # orphan sweep → empty

    session = MagicMock(spec=AsyncSession)
    # First call returns the existing run; second call (orphan sweep) returns empty.
    session.execute = AsyncMock(side_effect=[scalar, execute_result])
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# Import the private helper under test
# ---------------------------------------------------------------------------


from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db import persist as _persist  # noqa: E402


# ---------------------------------------------------------------------------
# _pr_number_from_url (used when persist_agent_event handles "done" with pr_url)
# ---------------------------------------------------------------------------


def test_pr_number_from_url_extracts_number() -> None:
    """PR number is parsed from GitHub PR URL so done events can update run.pr_number."""
    assert _persist._pr_number_from_url("https://github.com/owner/repo/pull/123") == 123
    assert _persist._pr_number_from_url("https://github.com/owner/repo/pull/123/") == 123
    assert _persist._pr_number_from_url("https://example.com/pulls/42") == 42


def test_pr_number_from_url_returns_none_for_invalid() -> None:
    """Non-URL or URL without trailing number returns None."""
    assert _persist._pr_number_from_url("") is None
    assert _persist._pr_number_from_url("https://github.com/owner/repo/pull") is None
    assert _persist._pr_number_from_url("not-a-url") is None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_launch_status_not_overwritten_by_poller() -> None:
    """Poller must not clobber pending_launch when it finds the worktree."""
    existing = _make_run(status="pending_launch")
    session = _make_session(existing)
    agent = _make_agent(status=AgentStatus.STALE)

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.status == "pending_launch", (
        "pending_launch was overwritten — Dispatcher queue drained by poller"
    )


@pytest.mark.anyio
async def test_implementing_status_is_updated_by_poller() -> None:
    """Non-pending_launch runs should still have their status updated normally."""
    existing = _make_run(status="implementing")
    session = _make_session(existing)
    agent = _make_agent(status=AgentStatus.STALE)

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.status == AgentStatus.STALE.value, (
        "implementing → stale transition should proceed normally"
    )


@pytest.mark.anyio
async def test_new_run_inserted_when_not_in_db() -> None:
    """When no existing row is found, a new ACAgentRun should be added."""
    session = _make_session(existing_run=None)
    agent = _make_agent(status=AgentStatus.IMPLEMENTING)

    await _persist._upsert_agent_runs(session, [agent])

    session.add.assert_called_once()
    inserted: ACAgentRun = session.add.call_args[0][0]
    assert inserted.id == agent.id
    assert inserted.status == AgentStatus.IMPLEMENTING.value
