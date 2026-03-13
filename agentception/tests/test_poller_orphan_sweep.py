from __future__ import annotations

"""Tests for the poller orphan sweep and build_complete_run event emission.

Covers:
- complete_agent_run() emits a build_complete_run event row.
- Orphan sweep marks a run failed when worktree is gone and no build_complete_run event exists.
- Orphan sweep leaves a run alone when a build_complete_run event is present.
- Orphan sweep skips reviewer runs entirely.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.models import ACAgentEvent, ACAgentRun
from agentception.db import persist as _persist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    status: str = "implementing",
    role: str = "developer",
    issue_number: int | None = 42,
) -> ACAgentRun:
    """Return a minimal ACAgentRun instance for testing."""
    run = ACAgentRun()
    run.id = f"issue-{uuid.uuid4().hex[:8]}"
    run.status = status
    run.role = role
    run.issue_number = issue_number
    run.pr_number = None
    run.worktree_path = f"/worktrees/{run.id}"
    run.branch = f"feat/{run.id}"
    run.batch_id = "batch-test"
    run.cognitive_arch = None
    run.last_activity_at = None
    run.completed_at = None
    return run


def _make_fake_session(run: ACAgentRun) -> MagicMock:
    """Return a mock AsyncSession that returns *run* from execute().scalar_one_or_none()."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# AC 1: complete_agent_run() emits a build_complete_run event
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_complete_agent_run_emits_build_complete_event() -> None:
    """complete_agent_run() inserts an ACAgentEvent with event_type='build_complete_run'."""
    run = _make_run(status="implementing")
    session = _make_fake_session(run)

    with patch("agentception.db.persist.get_session", return_value=session):
        ok = await _persist.complete_agent_run(run.id)

    assert ok is True
    assert run.status == "completed"

    # session.add must have been called with an ACAgentEvent of the right type.
    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    assert len(event_rows) == 1, f"Expected 1 ACAgentEvent, got {len(event_rows)}"
    assert event_rows[0].event_type == "build_complete_run"
    assert event_rows[0].agent_run_id == run.id


# ---------------------------------------------------------------------------
# AC 2: orphan sweep marks run failed when no build_complete_run event
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_build_complete_marks_failed() -> None:
    """Orphan with no build_complete_run event is marked failed by the sweep."""
    orphan = _make_run(status="implementing")

    # No live agents — orphan is not in live_ids.
    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [orphan]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    # Per-agent row lookup returns None (no existing run for the live agent).
    scalar_lookup = MagicMock()
    scalar_lookup.scalar_one_or_none.return_value = None

    session = MagicMock(spec=AsyncSession)
    # Call order: (1) per-agent lookup, (2) orphan sweep query, (3) TTL sweep query.
    session.execute = AsyncMock(
        side_effect=[scalar_lookup, orphan_result_mock, ttl_sweep_result]
    )
    # session.scalar() is used for the COUNT query inside the orphan sweep.
    session.scalar = AsyncMock(return_value=0)  # no build_complete_run event
    session.add = MagicMock()

    from agentception.models import AgentNode, AgentStatus

    different_agent = AgentNode(
        id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING
    )

    await _persist._upsert_agent_runs(session, [different_agent])

    assert orphan.status == "failed", (
        f"Expected orphan.status='failed', got {orphan.status!r}"
    )

    # An orphan_failed event must have been added.
    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    orphan_failed_events = [e for e in event_rows if e.event_type == "orphan_failed"]
    assert len(orphan_failed_events) == 1, (
        f"Expected 1 orphan_failed event, got {len(orphan_failed_events)}"
    )
    payload = json.loads(orphan_failed_events[0].payload)
    assert payload["reason"] == "worktree_gone_no_build_complete"


# ---------------------------------------------------------------------------
# AC 3: orphan sweep leaves run alone when build_complete_run event present
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_present_build_complete_not_refailed() -> None:
    """Orphan with a build_complete_run event is NOT mutated by the sweep."""
    # Status is already 'completed' — the sweep should not touch it.
    orphan = _make_run(status="completed")

    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [orphan]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar_lookup = MagicMock()
    scalar_lookup.scalar_one_or_none.return_value = None

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[scalar_lookup, orphan_result_mock, ttl_sweep_result]
    )
    # COUNT returns 1 — build_complete_run event exists.
    session.scalar = AsyncMock(return_value=1)
    session.add = MagicMock()

    from agentception.models import AgentNode, AgentStatus

    different_agent = AgentNode(
        id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING
    )

    await _persist._upsert_agent_runs(session, [different_agent])

    # Status must remain 'completed' — the sweep must not mutate it.
    assert orphan.status == "completed", (
        f"Expected orphan.status='completed', got {orphan.status!r}"
    )

    # No orphan_failed event should have been added.
    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    orphan_failed_events = [e for e in event_rows if e.event_type == "orphan_failed"]
    assert len(orphan_failed_events) == 0, (
        f"Expected no orphan_failed events, got {len(orphan_failed_events)}"
    )


# ---------------------------------------------------------------------------
# AC 6: reviewer runs are never mutated by the sweep
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reviewer_run_not_mutated() -> None:
    """Orphan sweep must skip reviewer runs regardless of event state."""
    reviewer = _make_run(status="implementing", role="reviewer")

    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [reviewer]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar_lookup = MagicMock()
    scalar_lookup.scalar_one_or_none.return_value = None

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[scalar_lookup, orphan_result_mock, ttl_sweep_result]
    )
    # Even if scalar() were called, return 0 (no event) — but it should NOT be called.
    session.scalar = AsyncMock(return_value=0)
    session.add = MagicMock()

    from agentception.models import AgentNode, AgentStatus

    different_agent = AgentNode(
        id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING
    )

    await _persist._upsert_agent_runs(session, [different_agent])

    # Reviewer run must be untouched.
    assert reviewer.status == "implementing", (
        f"Reviewer run was mutated: status={reviewer.status!r}"
    )

    # No orphan_failed event should have been added for the reviewer.
    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    orphan_failed_events = [e for e in event_rows if e.event_type == "orphan_failed"]
    assert len(orphan_failed_events) == 0, (
        f"Expected no orphan_failed events for reviewer, got {len(orphan_failed_events)}"
    )


# ---------------------------------------------------------------------------
# AC: no_autoflush prevents premature flush during multi-orphan sweep
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_autoflush_prevents_premature_flush_on_second_orphan() -> None:
    """Two orphan runs in the same session must not trigger autoflush on the second SELECT.

    Specification (safety property):
        For all iterations i of the orphan sweep loop, the SELECT issued in
        iteration i must NOT flush pending rows added in iteration i-1.
        The authoritative flush point is session.commit() at the end of
        persist_tick — no earlier flush is permitted.

    This test simulates the race by using a fake session whose no_autoflush
    context manager tracks entry/exit and whose scalar() call asserts that
    no_autoflush is active.  If the production code omits the
    ``with session.no_autoflush:`` block, the assertion fires.

    Both orphan runs must be marked 'failed' with 'orphan_failed' events.
    No SAWarning or flush-related exception must be raised.
    """
    import warnings

    import sqlalchemy.exc

    orphan1 = _make_run(status="implementing")
    orphan2 = _make_run(status="implementing")

    orphan_result_mock = MagicMock()
    orphan_result_mock.scalars.return_value.all.return_value = [orphan1, orphan2]

    ttl_sweep_result = MagicMock()
    ttl_sweep_result.scalars.return_value.all.return_value = []

    scalar_lookup = MagicMock()
    scalar_lookup.scalar_one_or_none.return_value = None

    # Track whether no_autoflush is active when scalar() is called.
    no_autoflush_active: list[bool] = []
    _no_autoflush_depth = [0]

    class _NoAutoflushCtx:
        """Synchronous context manager that tracks depth."""

        def __enter__(self) -> "_NoAutoflushCtx":
            _no_autoflush_depth[0] += 1
            return self

        def __exit__(self, *args: object) -> None:
            _no_autoflush_depth[0] -= 1

    async def _scalar_side_effect(stmt: object) -> int:
        # Record whether we are inside no_autoflush when scalar() fires.
        no_autoflush_active.append(_no_autoflush_depth[0] > 0)
        return 0  # no build_complete_run event — both orphans should be failed

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[scalar_lookup, orphan_result_mock, ttl_sweep_result]
    )
    session.scalar = AsyncMock(side_effect=_scalar_side_effect)
    session.add = MagicMock()
    # Attach the tracking no_autoflush context manager.
    session.no_autoflush = _NoAutoflushCtx()

    from agentception.models import AgentNode, AgentStatus

    different_agent = AgentNode(
        id="different-run-id", role="cto", status=AgentStatus.IMPLEMENTING
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", sqlalchemy.exc.SAWarning)
        await _persist._upsert_agent_runs(session, [different_agent])

    # Both orphans must be marked failed.
    assert orphan1.status == "failed", (
        f"orphan1.status expected 'failed', got {orphan1.status!r}"
    )
    assert orphan2.status == "failed", (
        f"orphan2.status expected 'failed', got {orphan2.status!r}"
    )

    # scalar() must have been called exactly twice (once per orphan).
    assert len(no_autoflush_active) == 2, (
        f"Expected scalar() called 2 times, got {len(no_autoflush_active)}"
    )

    # Both calls must have been inside no_autoflush.
    assert all(no_autoflush_active), (
        f"scalar() was called outside no_autoflush context: {no_autoflush_active}"
    )

    # Both orphan_failed events must have been added.
    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    orphan_failed_events = [e for e in event_rows if e.event_type == "orphan_failed"]
    assert len(orphan_failed_events) == 2, (
        f"Expected 2 orphan_failed events, got {len(orphan_failed_events)}"
    )

