from __future__ import annotations

"""Tests for build_complete_run reviewer worktree teardown / release behaviour.

Coverage:
- C/D/F grade: reviewer worktree is *released* (branch kept) before redispatch.
- A/B grade: reviewer worktree is fully torn down (branch deleted, PR merged).
- Teardown task name follows the expected convention.

Run targeted:
    pytest agentception/tests/test_build_commands.py -v
"""

import pytest
from unittest.mock import AsyncMock, patch

from agentception.tests.conftest import make_create_task_side_effect


@pytest.mark.anyio
async def test_reviewer_worktree_released_after_failing_grade() -> None:
    """build_complete_run releases (not full-tears-down) the reviewer worktree for C/D/F.

    For a failing grade, the PR branch must be kept alive so the re-dispatched
    developer can continue from the existing branch.  release_worktree removes
    the worktree directory without deleting the branch, while teardown_agent_worktree
    (which would delete the branch) must NOT be called for C/D/F grades.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-42-abc123"
    wt_path = "/worktrees/review-99"
    pr_branch = "feat/issue-42"

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
            "agentception.mcp.build_commands.get_agent_run_teardown",
            new_callable=AsyncMock,
            return_value={"worktree_path": wt_path, "branch": pr_branch},
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
        patch("agentception.mcp.build_commands.settings") as mock_settings,
    ):
        mock_settings.repo_dir = "/app"
        result = await build_complete_run(
            issue_number=42,
            pr_url="https://github.com/cgcardona/agentception/pull/99",
            agent_run_id=reviewer_run_id,
            grade="C",
            reviewer_feedback="Missing tests for the happy path.",
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # release_worktree is awaited synchronously (branch kept alive for developer).
    mock_release.assert_awaited_once_with(wt_path, "/app")

    # teardown_agent_worktree (which deletes the branch) must NOT be called for C/D/F.
    mock_teardown.assert_not_called()

    # No teardown task should be in create_task calls either.
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert f"teardown-{reviewer_run_id}" not in task_names


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
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
        patch(
            "agentception.mcp.build_commands._is_pr_merged",
            new_callable=AsyncMock,
            return_value=True,
        ),
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
    """build_complete_run releases reviewer worktree then schedules redispatch for grade F.

    Regression: a failing grade (C/D/F) from a reviewer must:
    1. Await release_worktree synchronously (keeps branch alive for continuation).
    2. Schedule auto_redispatch_after_rejection with the PR branch name so the
       re-dispatched developer attaches to the existing branch instead of starting
       from origin/dev.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-77-ghi789"
    issue_number = 77
    pr_url = "https://github.com/cgcardona/agentception/pull/200"
    reviewer_feedback = "1. Missing type hints\n2. No tests for failure path"
    grade = "F"
    pr_branch = "feat/issue-77"

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
            "agentception.mcp.build_commands.get_agent_run_teardown",
            new_callable=AsyncMock,
            return_value={"worktree_path": "/worktrees/review-200", "branch": pr_branch},
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_redispatch_after_rejection",
            new_callable=AsyncMock,
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
        patch("agentception.mcp.build_commands.settings") as mock_settings,
    ):
        mock_settings.repo_dir = "/app"
        result = await build_complete_run(
            issue_number=issue_number,
            pr_url=pr_url,
            agent_run_id=reviewer_run_id,
            grade=grade,
            reviewer_feedback=reviewer_feedback,
        )

    assert result["ok"] is True
    assert result["status"] == "completed"

    # release_worktree must be awaited (not fire-and-forget) to free the branch
    # before the developer continuation dispatch starts.
    mock_release.assert_awaited_once_with("/worktrees/review-200", "/app")

    # Verify that a redispatch task was scheduled.
    task_names = [
        c.kwargs.get("name", "") for c in mock_create_task.call_args_list
    ]
    assert f"auto-redispatch-{issue_number}" in task_names, (
        f"Expected auto-redispatch task for issue #{issue_number}; "
        f"got task names: {task_names}"
    )

    # Verify the coroutine passed to create_task was auto_redispatch_after_rejection
    # with the correct arguments including pr_branch for continuation.
    mock_redispatch.assert_called_once_with(
        issue_number=issue_number,
        pr_url=pr_url,
        reviewer_feedback=reviewer_feedback,
        grade="F",
        pr_branch=pr_branch,
    )


@pytest.mark.anyio
async def test_redispatch_skipped_after_passing_grade() -> None:
    """build_complete_run does NOT schedule auto_redispatch_after_rejection for grade A.

    A passing grade (A or B) means the reviewer already merged the PR.
    No developer re-dispatch should be triggered; instead a full teardown
    (deleting the branch) is queued because the branch is now merged.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-88-jkl012"
    issue_number = 88
    pr_url = "https://github.com/cgcardona/agentception/pull/300"

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
        ) as mock_redispatch,
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
        patch(
            "agentception.mcp.build_commands._is_pr_merged",
            new_callable=AsyncMock,
            return_value=True,
        ),
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

    # Full teardown (including branch deletion) must be queued for A/B grades.
    assert f"teardown-{reviewer_run_id}" in task_names, (
        f"Expected teardown task for reviewer; got task names: {task_names}"
    )

    # The redispatch mock should never have been called.
    mock_redispatch.assert_not_called()
    _ = mock_teardown  # verified via create_task names above


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
            side_effect=make_create_task_side_effect(),
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
async def test_build_complete_run_refused_invalid_pr_url() -> None:
    """build_complete_run refuses when pr_url is not a valid GitHub PR URL.

    Regression: the previous DB-based _has_pr_recorded guard was a deadlock —
    ACAgentRun.pr_number is only set by persist_agent_event(done), which runs
    *inside* build_complete_run after the guard, so the guard always returned
    False and agents looped to 100 iterations.

    The replacement guard validates the pr_url argument directly: if it looks
    like https://github.com/<owner>/<repo>/pull/<number> the agent has a PR.
    persist_agent_event must not be called on a refused run.
    """
    from agentception.mcp.build_commands import build_complete_run

    with patch(
        "agentception.mcp.build_commands.persist_agent_event",
        new_callable=AsyncMock,
    ) as mock_persist:
        result = await build_complete_run(
            issue_number=999,
            pr_url="",
            agent_run_id="issue-999-no-pr",
        )

    assert result["ok"] is False
    assert "pr_url" in str(result["error"]).lower()
    assert "create_pull_request" in str(result["error"])
    mock_persist.assert_not_called()


@pytest.mark.anyio
async def test_build_complete_run_accepted_with_valid_pr_url() -> None:
    """build_complete_run proceeds when pr_url is a valid GitHub PR URL.

    Ensures the URL guard passes for a well-formed URL so the completion
    path (persist_agent_event) is reached.
    """
    from agentception.mcp.build_commands import build_complete_run

    with (
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
            side_effect=make_create_task_side_effect(),
        ),
    ):
        result = await build_complete_run(
            issue_number=997,
            pr_url="https://github.com/cgcardona/agentception/pull/3",
            agent_run_id="issue-997-all-good",
        )

    assert result["ok"] is True
    assert result["status"] == "completed"
    mock_persist.assert_called_once()


@pytest.mark.anyio
async def test_implementer_completion_fails_when_release_worktree_returns_false() -> None:
    """build_complete_run returns error and does not dispatch reviewer when release_worktree fails.

    Regression: if git worktree remove fails, dispatching the reviewer would fail with
    'feat/issue-N is already used by worktree at /worktrees/issue-N'. We must not
    dispatch until the worktree is actually released.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "issue-939"
    wt_path = "/worktrees/issue-939"

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
            return_value="developer",
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_teardown",
            new_callable=AsyncMock,
            return_value={"worktree_path": wt_path, "branch": "feat/issue-939"},
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ) as mock_reviewer,
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
        patch(
            "agentception.mcp.build_commands._rebase_and_push_worktree",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await build_complete_run(
            issue_number=939,
            pr_url="https://github.com/cgcardona/agentception/pull/950",
            agent_run_id=agent_run_id,
        )

    assert result["ok"] is False
    assert "error" in result
    assert "worktree" in str(result["error"]).lower() or "release" in str(result["error"]).lower()
    mock_release.assert_awaited_once()
    mock_reviewer.assert_not_called()
    # Reviewer must not be dispatched (create_task with auto_dispatch_reviewer).
    task_calls = mock_create_task.call_args_list
    reviewer_calls = [c for c in task_calls if "reviewer" in str(c)]
    assert not reviewer_calls, "reviewer must not be dispatched when release_worktree fails"


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
            side_effect=make_create_task_side_effect(),
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


@pytest.mark.anyio
async def test_done_event_payload_includes_grade_for_reviewer() -> None:
    """Reviewer done-event payload must include grade and reviewer_feedback."""
    from agentception.mcp.build_commands import build_complete_run

    captured_payload: dict[str, str] = {}

    async def _capture_persist(
        *,
        issue_number: int,
        event_type: str,
        payload: dict[str, str],
        agent_run_id: str | None = None,
    ) -> None:
        if event_type == "done":
            captured_payload.update(payload)

    with (
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            side_effect=_capture_persist,
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
        ),
        patch("agentception.mcp.build_commands.asyncio.create_task", side_effect=make_create_task_side_effect()),
        patch(
            "agentception.mcp.build_commands._is_pr_merged",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await build_complete_run(
            issue_number=42,
            pr_url="https://github.com/cgcardona/agentception/pull/99",
            agent_run_id="review-issue-42-abc",
            grade="b",                          # lower-case — must be normalised
            reviewer_feedback="Looks good overall.",
        )

    assert captured_payload.get("grade") == "B"
    assert captured_payload.get("reviewer_feedback") == "Looks good overall."
    assert "pr_url" in captured_payload


@pytest.mark.anyio
async def test_done_event_payload_excludes_grade_for_developer() -> None:
    """Developer done-event payload must NOT contain grade or reviewer_feedback."""
    from agentception.mcp.build_commands import build_complete_run

    captured_payload: dict[str, str] = {}

    async def _capture_persist(
        *,
        issue_number: int,
        event_type: str,
        payload: dict[str, str],
        agent_run_id: str | None = None,
    ) -> None:
        if event_type == "done":
            captured_payload.update(payload)

    with (
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            side_effect=_capture_persist,
        ),
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
    ):
        await build_complete_run(
            issue_number=99,
            pr_url="https://github.com/cgcardona/agentception/pull/200",
            agent_run_id="issue-99-xyz",
        )

    assert "grade" not in captured_payload
    assert "reviewer_feedback" not in captured_payload
    assert "pr_url" in captured_payload


# ---------------------------------------------------------------------------
# Regression: reviewer must not trigger teardown when PR is not yet merged
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_complete_run_blocks_grade_a_when_pr_not_merged() -> None:
    """build_complete_run must refuse a grade-A completion when the PR is not merged.

    Regression for the bug where a reviewer called build_complete_run(grade="A")
    after merge_pull_request failed (branch behind dev).  The server accepted the
    completion, scheduled teardown_agent_worktree, which deleted the remote branch,
    which caused GitHub to auto-close the unmerged PR.

    After this fix, build_complete_run returns an error when _is_pr_merged is False,
    forcing the reviewer to merge first.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-99-unmerged"

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
            "agentception.mcp.build_commands._is_pr_merged",
            new_callable=AsyncMock,
            return_value=False,  # PR has NOT been merged
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=99,
            pr_url="https://github.com/cgcardona/agentception/pull/500",
            agent_run_id=reviewer_run_id,
            grade="A",
            reviewer_feedback="",
        )

    assert result.get("ok") is False, "Expected error when PR is not merged"
    assert "not been merged" in str(result.get("error", "")).lower(), (
        f"Error message should mention unmerged PR; got: {result.get('error')}"
    )
    # Teardown must NOT have been scheduled — that would delete the branch.
    mock_create_task.assert_not_called()


@pytest.mark.anyio
async def test_build_complete_run_allows_grade_a_when_pr_merged() -> None:
    """build_complete_run proceeds normally for grade-A when the PR is confirmed merged.

    Complement to the unmerged-PR regression test: when _is_pr_merged returns True,
    the completion is accepted and teardown is scheduled as normal.
    """
    from agentception.mcp.build_commands import build_complete_run

    reviewer_run_id = "reviewer-issue-55-merged"

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
            "agentception.mcp.build_commands._is_pr_merged",
            new_callable=AsyncMock,
            return_value=True,  # PR has been merged
        ),
        patch(
            "agentception.mcp.build_commands.teardown_agent_worktree",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
            side_effect=make_create_task_side_effect(),
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=55,
            pr_url="https://github.com/cgcardona/agentception/pull/501",
            agent_run_id=reviewer_run_id,
            grade="A",
            reviewer_feedback="",
        )

    assert result.get("ok") is True
    assert result.get("status") == "completed"
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert f"teardown-{reviewer_run_id}" in task_names, (
        f"Teardown must be scheduled when PR is merged; got: {task_names}"
    )

