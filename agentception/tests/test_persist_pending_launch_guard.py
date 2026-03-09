from __future__ import annotations

"""Regression tests for the pending_launch guard in db/persist.py.

Bug: the poller called _upsert_agent_runs() whenever it found a worktree on
the filesystem.  If that worktree belonged to a pending_launch run (Dispatcher
not yet invoked), the upsert would overwrite the precious pending_launch status
with whatever status the poller derived (typically "implementing"), draining the
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
    """Return a minimal ACAgentRun ORM object with the given status.

    issue_number is set to a non-None value because these tests simulate
    issue-backed runs.  Ad-hoc runs (issue_number=None) are intentionally
    excluded from the orphan sweep and have a separate lifecycle.
    """
    run = ACAgentRun(
        id="label-developer-experience-layer-5492de",
        role="cto",
        status=status,
        issue_number=1,
        branch="agent/developer-experience-layer-b735",
        worktree_path="/worktrees/label-developer-experience-layer-5492de",
        spawned_at=datetime.datetime.now(datetime.timezone.utc),
    )
    return run


def _make_agent(status: AgentStatus = AgentStatus.IMPLEMENTING) -> AgentNode:
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

    orphan_sweep_result = MagicMock()
    orphan_sweep_result.scalars.return_value.all.return_value = []  # orphan sweep → empty

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []  # TTL sweep → empty

    session = MagicMock(spec=AsyncSession)
    # Call 1: per-agent row lookup; call 2: orphan sweep; call 3: pending_launch TTL sweep.
    session.execute = AsyncMock(side_effect=[scalar, orphan_sweep_result, ttl_sweep_result])
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
    agent = _make_agent(status=AgentStatus.IMPLEMENTING)

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.status == "pending_launch", (
        "pending_launch was overwritten — Dispatcher queue drained by poller"
    )


@pytest.mark.anyio
async def test_implementing_status_is_updated_by_poller() -> None:
    """Non-pending_launch runs should still have their status updated normally."""
    existing = _make_run(status="implementing")
    session = _make_session(existing)
    agent = _make_agent(status=AgentStatus.IMPLEMENTING)

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.status == AgentStatus.IMPLEMENTING.value, (
        "implementing status should be updated by poller normally"
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


# ---------------------------------------------------------------------------
# Regression: pr_number must not be overwritten with None by the poller
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pr_number_not_regressed_to_none_by_poller() -> None:
    """Kanban regression: engineer opens PR, pr_number is saved by build_report_done.

    On the very next tick the poller scans the DB (before the PR existed,
    so pr_number is None) and must NOT overwrite the saved pr_number.
    Without the guard the Kanban card collapses back to "todo"
    immediately after the engineer completes.
    """
    existing = _make_run(status="reviewing")
    existing.pr_number = 42  # set by persist_agent_event(done) earlier
    session = _make_session(existing)

    # Poller-synthesised AgentNode before the PR is created — pr_number is None.
    agent = _make_agent(status=AgentStatus.REVIEWING)
    assert agent.pr_number is None  # precondition

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.pr_number == 42, (
        "pr_number was regressed to None — Kanban card would return to todo lane"
    )


@pytest.mark.anyio
async def test_pr_number_advanced_when_poller_sees_one() -> None:
    """pr_number observed by the poller (non-None) must still be written to the DB."""
    existing = _make_run(status="implementing")
    existing.pr_number = None
    session = _make_session(existing)

    agent = AgentNode(
        id=existing.id,
        role="developer",
        status=AgentStatus.REVIEWING,
        pr_number=99,  # set e.g. by a build_report_done event
    )

    await _persist._upsert_agent_runs(session, [agent])

    assert existing.pr_number == 99


# ---------------------------------------------------------------------------
# Regression: orphan sweep must not drop runs that have an open PR
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orphan_with_pr_number_set_to_done() -> None:
    """Kanban regression: worktree removed, PR exists — card should land in PR Open lane.

    When the engineer's worktree disappears the orphan sweep sets status to
    'done' (not 'unknown') when pr_number is set.  'done' agent_status + a
    pr_number causes the template to route the card to the PR Open bucket,
    keeping it visible.  'unknown' would collapse it back to the todo lane.
    """
    orphan = _make_run(status="reviewing")
    orphan.pr_number = 77
    # Orphan has no live worktree: it is NOT in live_ids.
    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [orphan]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = None  # no existing run for the agent

    session = MagicMock(spec=AsyncSession)
    # Call 1: per-agent row lookup; call 2: orphan sweep; call 3: TTL sweep.
    session.execute = AsyncMock(side_effect=[scalar, orphan_result_mock, ttl_sweep_result])
    session.add = MagicMock()

    # Pass an agent with a DIFFERENT id so the orphan is never in live_ids.
    different_agent = AgentNode(id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING)

    await _persist._upsert_agent_runs(session, [different_agent])

    assert orphan.status == "completed", (
        "Orphan with open PR must be set to 'completed' so the Kanban card lands in PR Open"
    )


@pytest.mark.anyio
async def test_orphan_without_pr_number_flipped_to_failed() -> None:
    """Worktrees removed with no open PR should become failed (normal cleanup)."""
    orphan = _make_run(status="implementing")
    orphan.pr_number = None
    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [orphan]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = None

    session = MagicMock(spec=AsyncSession)
    # Call 1: per-agent row lookup; call 2: orphan sweep; call 3: TTL sweep.
    session.execute = AsyncMock(side_effect=[scalar, orphan_result_mock, ttl_sweep_result])
    session.add = MagicMock()

    different_agent = AgentNode(id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING)

    await _persist._upsert_agent_runs(session, [different_agent])

    assert orphan.status == "failed", (
        "Orphan without PR should be flipped to failed for cleanup"
    )


# ---------------------------------------------------------------------------
# Regression: pending_launch TTL sweep
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_launch_ttl_expired_run_flipped_to_failed() -> None:
    """A pending_launch run older than 15 min with no live worktree becomes failed.

    Dispatcher that aborts before acknowledging would otherwise lock the issue
    in the 'active' swim lane forever with no worktree to back it.
    """
    stale_pending = _make_run(status="pending_launch")
    stale_pending.spawned_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=30
    )

    orphan_sweep_result = MagicMock()
    orphan_sweep_result.scalars.return_value.all.return_value = []  # no orphans

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = [stale_pending]

    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = None  # no existing run for the live agent

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[scalar, orphan_sweep_result, ttl_sweep_result])
    session.add = MagicMock()

    different_agent = AgentNode(id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING)
    await _persist._upsert_agent_runs(session, [different_agent])

    assert stale_pending.status == "failed", (
        "Expired pending_launch must become failed so the issue returns to todo lane"
    )


@pytest.mark.anyio
async def test_pending_launch_recent_run_not_expired() -> None:
    """A recently queued pending_launch run must NOT be touched by the TTL sweep."""
    fresh_pending = _make_run(status="pending_launch")
    fresh_pending.spawned_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=5
    )

    orphan_sweep_result = MagicMock()
    orphan_sweep_result.scalars.return_value.all.return_value = []

    ttl_sweep_result = MagicMock()
    # TTL sweep query only returns runs older than the cutoff — fresh run not included.
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = None

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[scalar, orphan_sweep_result, ttl_sweep_result])
    session.add = MagicMock()

    different_agent = AgentNode(id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING)
    await _persist._upsert_agent_runs(session, [different_agent])

    assert fresh_pending.status == "pending_launch", (
        "Fresh pending_launch must not be touched by the TTL sweep"
    )


# ---------------------------------------------------------------------------
# Regression tests for new state-transition persist functions (PR 1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_complete_agent_run_transitions_implementing_to_completed() -> None:
    """complete_agent_run: implementing → completed succeeds."""
    import agentception.db.persist as _p

    run = _make_run(status="implementing")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    fake_session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.complete_agent_run("test-run-id")

    assert ok is True
    assert run.status == "completed"


@pytest.mark.anyio
async def test_complete_agent_run_rejects_non_implementing_state() -> None:
    """complete_agent_run: only succeeds from implementing state."""
    import agentception.db.persist as _p

    run = _make_run(status="blocked")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.complete_agent_run("test-run-id")

    assert ok is False
    assert run.status == "blocked"


@pytest.mark.anyio
async def test_block_agent_run_transitions_implementing_to_blocked() -> None:
    """block_agent_run: implementing → blocked succeeds."""
    import agentception.db.persist as _p

    run = _make_run(status="implementing")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    fake_session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.block_agent_run("test-run-id")

    assert ok is True
    assert run.status == "blocked"


@pytest.mark.anyio
async def test_resume_agent_run_transitions_blocked_to_implementing() -> None:
    """resume_agent_run: blocked → implementing succeeds."""
    import agentception.db.persist as _p

    run = _make_run(status="blocked")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    fake_session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.resume_agent_run("test-run-id", "some-agent-id")

    assert ok is True
    assert run.status == "implementing"


@pytest.mark.anyio
async def test_resume_agent_run_idempotent_if_already_implementing_same_id() -> None:
    """resume_agent_run: already implementing with same run_id → ok (restart-safe)."""
    import agentception.db.persist as _p

    run = _make_run(status="implementing")
    run.id = "label-developer-experience-layer-5492de"

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.resume_agent_run(
            "label-developer-experience-layer-5492de",
            "label-developer-experience-layer-5492de",
        )

    assert ok is True
    assert run.status == "implementing"


@pytest.mark.anyio
async def test_cancel_agent_run_transitions_implementing_to_cancelled() -> None:
    """cancel_agent_run: implementing → cancelled succeeds."""
    import agentception.db.persist as _p

    run = _make_run(status="implementing")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    fake_session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.cancel_agent_run("test-run-id")

    assert ok is True
    assert run.status == "cancelled"


@pytest.mark.anyio
async def test_stop_agent_run_transitions_any_active_to_stopped() -> None:
    """stop_agent_run: any active state → stopped."""
    import agentception.db.persist as _p

    run = _make_run(status="blocked")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    fake_session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.stop_agent_run("test-run-id")

    assert ok is True
    assert run.status == "stopped"


@pytest.mark.anyio
async def test_cancel_agent_run_rejects_terminal_state() -> None:
    """cancel_agent_run: cannot cancel a completed run."""
    import agentception.db.persist as _p

    run = _make_run(status="completed")

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )

    with patch("agentception.db.persist.get_session", return_value=fake_session):
        ok = await _p.cancel_agent_run("test-run-id")

    assert ok is False
    assert run.status == "completed"
