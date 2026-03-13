from __future__ import annotations

"""Tests for build_complete_run pre-flight guard clauses.

Coverage:
- build_complete_run refused when no file edits recorded for the run.
- build_complete_run refused when file edits exist but pr_number is NULL.
- build_complete_run allowed when both conditions are satisfied.

Run targeted:
    pytest tests/test_build_commands.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.anyio
async def test_build_complete_run_refused_no_file_edits() -> None:
    """build_complete_run returns refusal dict when no file_edit/write_file events exist.

    Invariant: an agent that has not written any files has produced no work
    product.  Accepting the call would trigger the auto-reviewer against an
    empty branch, wasting a full reviewer turn.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "issue-801-test-no-edits"

    # Mock the DB session to return count=0 for file_edit/write_file events.
    mock_scalar_result = MagicMock()
    mock_scalar_result.scalar_one.return_value = 0

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_scalar_result)

    with patch(
        "agentception.mcp.build_commands.get_session",
        return_value=mock_session,
    ):
        result = await build_complete_run(
            issue_number=801,
            pr_url="https://github.com/cgcardona/agentception/pull/999",
            agent_run_id=agent_run_id,
        )

    assert result == {
        "ok": False,
        "error": (
            "build_complete_run refused: no file edits recorded for this run. "
            "Write and commit your changes first."
        ),
    }


@pytest.mark.anyio
async def test_build_complete_run_refused_no_pr() -> None:
    """build_complete_run returns refusal dict when file edits exist but pr_number is NULL.

    Invariant: the auto-reviewer needs an open PR to review.  If no PR has
    been opened yet, the call must be rejected so the agent opens the PR first.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "issue-801-test-no-pr"

    # First session call: file_edit count > 0.
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 3

    # Second session call: ACAgentRun row with pr_number=None.
    mock_run_row = MagicMock()
    mock_run_row.pr_number = None

    mock_run_result = MagicMock()
    mock_run_result.scalar_one_or_none.return_value = mock_run_row

    # Two separate session context managers — one per `async with get_session()`.
    mock_session_1 = AsyncMock()
    mock_session_1.__aenter__ = AsyncMock(return_value=mock_session_1)
    mock_session_1.__aexit__ = AsyncMock(return_value=False)
    mock_session_1.execute = AsyncMock(return_value=mock_count_result)

    mock_session_2 = AsyncMock()
    mock_session_2.__aenter__ = AsyncMock(return_value=mock_session_2)
    mock_session_2.__aexit__ = AsyncMock(return_value=False)
    mock_session_2.execute = AsyncMock(return_value=mock_run_result)

    call_count = 0

    def session_factory() -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_session_1
        return mock_session_2

    with patch(
        "agentception.mcp.build_commands.get_session",
        side_effect=session_factory,
    ):
        result = await build_complete_run(
            issue_number=801,
            pr_url="https://github.com/cgcardona/agentception/pull/999",
            agent_run_id=agent_run_id,
        )

    assert result == {
        "ok": False,
        "error": (
            "build_complete_run refused: no PR found. "
            "Create and push a branch, then open a pull request first."
        ),
    }


@pytest.mark.anyio
async def test_build_complete_run_allowed_when_checks_pass() -> None:
    """build_complete_run proceeds to completion logic when both pre-flight checks pass.

    When file edits exist AND pr_number is set, the handler must reach the
    existing completion path (persist_agent_event is the first side-effectful
    call after the guards).
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "issue-801-test-allowed"

    # First session call: file_edit count > 0.
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 5

    # Second session call: ACAgentRun row with pr_number set.
    mock_run_row = MagicMock()
    mock_run_row.pr_number = 42

    mock_run_result = MagicMock()
    mock_run_result.scalar_one_or_none.return_value = mock_run_row

    mock_session_1 = AsyncMock()
    mock_session_1.__aenter__ = AsyncMock(return_value=mock_session_1)
    mock_session_1.__aexit__ = AsyncMock(return_value=False)
    mock_session_1.execute = AsyncMock(return_value=mock_count_result)

    mock_session_2 = AsyncMock()
    mock_session_2.__aenter__ = AsyncMock(return_value=mock_session_2)
    mock_session_2.__aexit__ = AsyncMock(return_value=False)
    mock_session_2.execute = AsyncMock(return_value=mock_run_result)

    call_count = 0

    def session_factory() -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_session_1
        return mock_session_2

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            side_effect=session_factory,
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist,
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="developer",
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_teardown",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ),
    ):
        result = await build_complete_run(
            issue_number=801,
            pr_url="https://github.com/cgcardona/agentception/pull/999",
            agent_run_id=agent_run_id,
        )

    # The handler must have reached the completion path.
    mock_persist.assert_awaited_once()
    assert result == {"ok": True, "event": "done", "status": "completed"}
