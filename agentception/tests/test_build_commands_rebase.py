from __future__ import annotations

"""Unit tests for the rebase-onto-dev logic inside build_complete_run.

The non-reviewer (implementer) path of build_complete_run:
1. Fetches origin/dev.
2. Rebases the feature branch onto origin/dev.
3a. On success: force-pushes the rebased branch, releases the worktree,
    and dispatches the auto-reviewer.
3b. On failure: aborts the rebase and returns a structured error dict
    with reason="rebase_conflict".

Coverage:
- test_rebase_succeeds_force_pushes_and_dispatches_reviewer
    Happy path: rebase exits 0 → force-push runs, worktree released,
    reviewer task created.
- test_rebase_conflict_returns_error_and_aborts
    Failure path: rebase exits non-zero → abort runs, error dict returned,
    reviewer task NOT created.
- test_no_worktree_path_skips_rebase_and_dispatches_reviewer
    When get_agent_run_teardown returns no worktree_path the rebase block
    is skipped entirely and the reviewer is still dispatched.

Run targeted:
    pytest agentception/tests/test_build_commands_rebase.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    """Return a mock subprocess whose communicate() returns (stdout, stderr)."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rebase_succeeds_force_pushes_and_dispatches_reviewer() -> None:
    """Happy path: rebase exits 0 → force-push, worktree release, reviewer dispatch.

    When the implementer calls build_complete_run and the rebase onto
    origin/dev succeeds, the function must:
    - force-push the rebased branch with --force-with-lease
    - release the worktree via release_worktree
    - schedule the auto-reviewer task
    - return {"ok": True, "event": "done", "status": "completed"}
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-10-abc"
    wt_path = "/worktrees/issue-10"
    branch_name = "feat/issue-10"

    # Subprocess sequence: fetch, rebase, rev-parse, push
    fetch_proc = _make_proc(0)
    rebase_proc = _make_proc(0)
    rev_parse_proc = _make_proc(0, stdout=f"{branch_name}\n".encode())
    push_proc = _make_proc(0)

    subprocess_calls = iter([fetch_proc, rebase_proc, rev_parse_proc, push_proc])

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> AsyncMock:
        return next(subprocess_calls)

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
            return_value={"worktree_path": wt_path},
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=10,
            pr_url="https://github.com/cgcardona/agentception/pull/10",
            agent_run_id=agent_run_id,
        )

    assert result == {"ok": True, "event": "done", "status": "completed"}

    # release_worktree must have been called with the worktree path.
    mock_release.assert_awaited_once()
    call_kwargs = mock_release.call_args
    assert call_kwargs.kwargs.get("worktree_path") == wt_path or (
        len(call_kwargs.args) > 0 and call_kwargs.args[0] == wt_path
    )

    # auto-reviewer task must have been scheduled.
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert "auto-reviewer-10" in task_names, (
        f"Expected auto-reviewer-10 task; got: {task_names}"
    )


@pytest.mark.anyio
async def test_rebase_conflict_returns_error_and_aborts() -> None:
    """Failure path: rebase exits non-zero → abort runs, error dict returned.

    When the rebase onto origin/dev fails (non-zero exit code), build_complete_run
    must:
    - run `git rebase --abort` to clean up the in-progress rebase
    - return a structured error dict with status="error" and reason="rebase_conflict"
    - NOT schedule the auto-reviewer task
    - NOT call release_worktree
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-20-conflict"
    wt_path = "/worktrees/issue-20"

    fetch_proc = _make_proc(0)
    rebase_proc = _make_proc(1, stderr=b"CONFLICT (content): Merge conflict in foo.py")
    abort_proc = _make_proc(0)

    subprocess_calls = iter([fetch_proc, rebase_proc, abort_proc])

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> AsyncMock:
        return next(subprocess_calls)

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
            return_value={"worktree_path": wt_path},
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
        patch(
            "agentception.mcp.build_commands.Path",
        ) as mock_path,
    ):
        # Make Path(wt_path).exists() return True so the rebase logic runs.
        mock_path.return_value.exists.return_value = True
        result = await build_complete_run(
            issue_number=20,
            pr_url="https://github.com/cgcardona/agentception/pull/20",
            agent_run_id=agent_run_id,
        )

    # Must return a structured error — not the success dict.
    assert result.get("status") == "error", f"Expected status=error, got: {result}"
    assert result.get("reason") == "rebase_conflict", (
        f"Expected reason=rebase_conflict, got: {result}"
    )
    assert "Rebase onto origin/dev failed" in str(result.get("message", "")), (
        f"Expected helpful message, got: {result}"
    )

    # release_worktree must NOT have been called.
    mock_release.assert_not_awaited()

    # auto-reviewer task must NOT have been scheduled.
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert "auto-reviewer-20" not in task_names, (
        f"Expected NO auto-reviewer task after rebase conflict; got: {task_names}"
    )


@pytest.mark.anyio
async def test_no_worktree_path_skips_rebase_and_dispatches_reviewer() -> None:
    """When teardown info has no worktree_path, rebase is skipped and reviewer fires.

    If get_agent_run_teardown returns None or a dict without 'worktree_path',
    the rebase block must be skipped entirely.  The auto-reviewer task must
    still be scheduled so the review pipeline is not silently broken.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-30-no-wt"

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
            return_value=None,  # no teardown info at all
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_subprocess_exec",
        ) as mock_subprocess,
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=30,
            pr_url="https://github.com/cgcardona/agentception/pull/30",
            agent_run_id=agent_run_id,
        )

    assert result == {"ok": True, "event": "done", "status": "completed"}

    # No subprocess calls should have been made (rebase skipped).
    mock_subprocess.assert_not_called()

    # release_worktree must NOT have been called (nothing to release).
    mock_release.assert_not_awaited()

    # auto-reviewer task must still be scheduled.
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert "auto-reviewer-30" in task_names, (
        f"Expected auto-reviewer-30 task even without worktree; got: {task_names}"
    )


@pytest.mark.anyio
async def test_rebase_succeeds_with_empty_worktree_path_dict() -> None:
    """When teardown dict has worktree_path=None, rebase is skipped gracefully.

    get_agent_run_teardown may return a dict where 'worktree_path' is None
    (e.g. the run was never assigned a worktree).  The rebase block must be
    skipped and the reviewer must still be dispatched.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-40-null-wt"

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
            return_value={"worktree_path": None},
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_subprocess_exec",
        ) as mock_subprocess,
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.asyncio.create_task",
        ) as mock_create_task,
    ):
        result = await build_complete_run(
            issue_number=40,
            pr_url="https://github.com/cgcardona/agentception/pull/40",
            agent_run_id=agent_run_id,
        )

    assert result == {"ok": True, "event": "done", "status": "completed"}

    # No subprocess calls — rebase was skipped.
    mock_subprocess.assert_not_called()
    mock_release.assert_not_awaited()

    # Reviewer still dispatched.
    task_names = [c.kwargs.get("name", "") for c in mock_create_task.call_args_list]
    assert "auto-reviewer-40" in task_names, (
        f"Expected auto-reviewer-40 task; got: {task_names}"
    )
