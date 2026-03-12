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
from unittest.mock import AsyncMock, patch


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
