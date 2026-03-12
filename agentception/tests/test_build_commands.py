from __future__ import annotations

"""Tests for build_complete_run reviewer worktree teardown behaviour.

Coverage:
- Reviewer worktree is torn down after a failing grade (C/D/F).
- Reviewer worktree is torn down after a passing grade (A/B).
- Teardown task name follows the expected convention.

Run targeted:
    pytest agentception/tests/test_build_commands.py -v
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_session(file_edit_count: int = 1, pr_number: int | None = 42) -> MagicMock:
    """Build a mock async context-manager session that satisfies the pre-flight guards.

    The pre-flight guards in build_complete_run execute two queries:
    1. COUNT of ACAgentEvent rows with event_type in ('file_edit', 'write_file').
    2. SELECT of ACAgentRun row to check pr_number IS NOT NULL.

    This helper returns a mock session whose ``execute`` method returns the
    appropriate scalar results in call order.
    """
    mock_session = MagicMock()

    # First execute call → file_edit count
    count_result = MagicMock()
    count_result.scalar_one.return_value = file_edit_count

    # Second execute call → ACAgentRun row
    run_row = MagicMock()
    run_row.pr_number = pr_number
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run_row

    mock_session.execute = AsyncMock(side_effect=[count_result, run_result])

    from collections.abc import AsyncGenerator

    @asynccontextmanager
    async def _mock_get_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    return _mock_get_session  # type: ignore[return-value]


@pytest.mark.anyio
async def test_reviewer_worktree_torn_down_after_failing_grade() -> None:
    """build_complete_run schedules reviewer worktree teardown on a failing grade (C).

    Regression: the reviewer worktree was left on disk after a C/D/F grade,
    blocking re-dispatch of the same issue because git worktree add refuses to
    check out a branch already active in another worktree.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-42-abc123"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=99),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=42,
            pr_url="https://github.com/cgcardona/agentception/pull/99",
            agent_run_id=reviewer_run_id,
            grade="C",
            reviewer_feedback="Missing tests for the happy path.",
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # Verify that a teardown task was scheduled for the reviewer's run_id.
    task_names = [
        c.kwargs.get("name", "") for c in mock_create_task.call_args_list
    ]
    assert f"teardown-{reviewer_run_id}" in task_names, (
        f"Expected teardown task for reviewer run_id={reviewer_run_id!r}; "
        f"got task names: {task_names}"
    )


@pytest.mark.anyio
async def test_reviewer_worktree_torn_down_after_passing_grade() -> None:
    """build_complete_run schedules reviewer worktree teardown on a passing grade (A).

    Teardown must be unconditional — not only on failing grades — so the
    reviewer worktree is always cleaned up and never blocks future dispatches.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-55-def456"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=100),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=55,
            pr_url="https://github.com/cgcardona/agentception/pull/100",
            agent_run_id=reviewer_run_id,
            grade="A",
            reviewer_feedback="",
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # Verify that a teardown task was scheduled for the reviewer's run_id.
    task_names = [
        c.kwargs.get("name", "") for c in mock_create_task.call_args_list
    ]
    assert f"teardown-{reviewer_run_id}" in task_names, (
        f"Expected teardown task for reviewer run_id={reviewer_run_id!r}; "
        f"got task names: {task_names}"
    )


@pytest.mark.anyio
async def test_redispatch_fires_after_failing_grade() -> None:
    """build_complete_run schedules auto_redispatch_after_rejection when grade is F.

    Regression: a failing grade (C/D/F) from a reviewer must automatically
    re-queue the original issue to a fresh developer worktree via
    auto_redispatch_after_rejection.  The task must be created with the
    correct issue_number, pr_url, reviewer_feedback, and grade.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-77-ghi789"
    issue_number = 77
    pr_url = "https://github.com/cgcardona/agentception/pull/200"
    reviewer_feedback = "1. Missing type hints\n2. No tests for failure path"
    grade = "F"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=200),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=issue_number,
            pr_url=pr_url,
            agent_run_id=reviewer_run_id,
            grade=grade,
            reviewer_feedback=reviewer_feedback,
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # Verify that a redispatch task was scheduled.
    task_names = [
        c.kwargs.get("name", "") for c in mock_create_task.call_args_list
    ]
    assert f"auto-redispatch-{issue_number}" in task_names, (
        f"Expected auto-redispatch task for issue #{issue_number}; "
        f"got task names: {task_names}"
    )

    # Verify the coroutine passed to create_task was auto_redispatch_after_rejection
    # with the correct arguments.
    mock_redispatch.assert_called_once_with(
        issue_number=issue_number,
        pr_url=pr_url,
        reviewer_feedback=reviewer_feedback,
        grade="F",
    )


@pytest.mark.anyio
async def test_redispatch_skipped_after_passing_grade() -> None:
    """build_complete_run does NOT schedule auto_redispatch_after_rejection for grade A.

    A passing grade (A or B) means the reviewer already merged the PR.
    No developer re-dispatch should be triggered.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-88-jkl012"
    issue_number = 88
    pr_url = "https://github.com/cgcardona/agentception/pull/300"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=300),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=issue_number,
            pr_url=pr_url,
            agent_run_id=reviewer_run_id,
            grade="A",
            reviewer_feedback="",
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # Verify that NO redispatch task was scheduled.
    task_names = [
        c.kwargs.get("name", "") for c in mock_create_task.call_args_list
    ]
    assert f"auto-redispatch-{issue_number}" not in task_names, (
        f"Expected NO auto-redispatch task for grade A; "
        f"got task names: {task_names}"
    )

    # The mock should never have been called (create_task wraps the coroutine).
    mock_redispatch.assert_not_called()


@pytest.mark.anyio
async def test_build_complete_run_rejects_empty_grade_from_reviewer() -> None:
    """build_complete_run returns an error dict when reviewer passes grade=''.

    Regression: an empty grade must be caught before merge/redispatch logic
    runs so the LLM sees a structured error and can retry with a valid grade.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-99-empty"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=999),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=99,
            pr_url="https://github.com/cgcardona/agentception/pull/999",
            agent_run_id=reviewer_run_id,
            grade="",
            reviewer_feedback="",
        )

    assert "error" in result
    assert "A, B, C, D, F" in str(result["error"])
    mock_redispatch.assert_not_called()
    mock_teardown.assert_not_called()
    mock_create_task.assert_not_called()


@pytest.mark.anyio
async def test_build_complete_run_rejects_whitespace_grade_from_reviewer() -> None:
    """build_complete_run returns an error dict when reviewer passes grade='   '.

    Whitespace-only input must be caught after normalisation (strip + upper)
    the same way an empty string is caught.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-99-whitespace"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_mock_session(file_edit_count=1, pr_number=999),
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="reviewer",
        ),
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=99,
            pr_url="https://github.com/cgcardona/agentception/pull/999",
            agent_run_id=reviewer_run_id,
            grade="   ",
            reviewer_feedback="",
        )

    assert "error" in result
    assert "A, B, C, D, F" in str(result["error"])
    mock_redispatch.assert_not_called()
    mock_teardown.assert_not_called()
    mock_create_task.assert_not_called()

