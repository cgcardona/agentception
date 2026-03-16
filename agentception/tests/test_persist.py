from __future__ import annotations

"""Tests for agentception/db/persist.py.

Covers the public helpers that are exercised by the phase-0 bug fixes:
- complete_agent_run() transitions status and emits build_complete_run event.
- persist_agent_event() inserts an ACAgentEvent row with the correct fields.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.models import ACAgentEvent, ACAgentRun
from agentception.db import persist as _persist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(status: str = "implementing") -> ACAgentRun:
    run = ACAgentRun()
    run.id = "issue-test-001"
    run.status = status
    run.role = "developer"
    run.issue_number = 1
    run.pr_number = None
    run.worktree_path = "/worktrees/issue-test-001"
    run.branch = "agent/issue-test-001"
    run.batch_id = "batch-test"
    run.cognitive_arch = None
    run.last_activity_at = None
    run.completed_at = None
    return run


def _make_fake_session(run: ACAgentRun) -> MagicMock:
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
# complete_agent_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_complete_agent_run_returns_true_and_sets_completed() -> None:
    """complete_agent_run() returns True and sets status='completed'."""
    run = _make_run(status="implementing")
    session = _make_fake_session(run)

    with patch("agentception.db.persist.get_session", return_value=session):
        result = await _persist.complete_agent_run(run.id)

    assert result is True
    assert run.status == "completed"
    assert run.completed_at is not None


@pytest.mark.anyio
async def test_complete_agent_run_emits_build_complete_run_event() -> None:
    """complete_agent_run() inserts an ACAgentEvent with event_type='build_complete_run'."""
    run = _make_run(status="implementing")
    session = _make_fake_session(run)

    with patch("agentception.db.persist.get_session", return_value=session):
        await _persist.complete_agent_run(run.id)

    added_objects = [call.args[0] for call in session.add.call_args_list]
    event_rows = [o for o in added_objects if isinstance(o, ACAgentEvent)]
    assert len(event_rows) == 1, f"Expected 1 ACAgentEvent, got {len(event_rows)}"
    assert event_rows[0].event_type == "build_complete_run"
    assert event_rows[0].agent_run_id == run.id


@pytest.mark.anyio
async def test_complete_agent_run_returns_false_when_not_implementing() -> None:
    """complete_agent_run() returns False when the run is not in 'implementing' state."""
    run = _make_run(status="completed")
    session = _make_fake_session(run)

    with patch("agentception.db.persist.get_session", return_value=session):
        result = await _persist.complete_agent_run(run.id)

    assert result is False
    # Status must not be mutated.
    assert run.status == "completed"


@pytest.mark.anyio
async def test_complete_agent_run_returns_false_when_run_not_found() -> None:
    """complete_agent_run() returns False when the run_id does not exist."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=session):
        result = await _persist.complete_agent_run("nonexistent-run-id")

    assert result is False


# ---------------------------------------------------------------------------
# persist_agent_event
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_persist_agent_event_inserts_row_with_correct_fields() -> None:
    """persist_agent_event() inserts an ACAgentEvent with the expected field values."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=session):
        await _persist.persist_agent_event(
            issue_number=42,
            event_type="step_start",
            payload={"step": "Reading codebase"},
            agent_run_id="issue-42",
        )

    session.add.assert_called_once()
    event = session.add.call_args.args[0]
    assert isinstance(event, ACAgentEvent)
    assert event.event_type == "step_start"
    assert event.issue_number == 42
    assert event.agent_run_id == "issue-42"
    stored_payload = json.loads(event.payload)
    assert stored_payload == {"step": "Reading codebase"}


@pytest.mark.anyio
async def test_persist_agent_event_payload_is_json_serialisable() -> None:
    """persist_agent_event() stores payload as a JSON string, not a raw dict."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("agentception.db.persist.get_session", return_value=session):
        await _persist.persist_agent_event(
            issue_number=1,
            event_type="file_edit",
            payload={"path": "foo.py", "timestamp": "2024-01-01T12:00:00"},
            agent_run_id="issue-1",
        )

    event = session.add.call_args.args[0]
    # payload must be a string — json.loads must succeed without raising.
    assert isinstance(event.payload, str)
    parsed = json.loads(event.payload)
    assert parsed["path"] == "foo.py"
    assert parsed["timestamp"] == "2024-01-01T12:00:00"
