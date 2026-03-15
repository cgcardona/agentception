from __future__ import annotations

"""Tests for the worktree reaper.

Critical invariants:
- Issue-scoped runs (issue-*): reaper calls release_worktree only (no branch
  deletion), never teardown_agent_worktree, so open PRs are not closed.
- Label/coordinator runs (label-*): reaper also deletes the remote and local
  branch, which never backs a PR and would otherwise accumulate on GitHub.
- When a worktree directory is already gone, the reaper must clear the DB ref
  so the run never appears in future reaper passes (the stale-loop bug).
- _reaper_loop wraps reap_stale_worktrees in try/except so one bad pass never
  kills the background loop permanently.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.anyio
async def test_reaper_calls_release_not_teardown(tmp_path: Path) -> None:
    """Reaper calls release_worktree, never teardown_agent_worktree.

    Regression for the bug where reap_stale_worktrees called teardown_agent_worktree,
    which deleted the remote branch and caused GitHub to auto-close open PRs.
    """
    fake_worktree = tmp_path / "issue-99"
    fake_worktree.mkdir()

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{"id": "issue-99", "worktree_path": str(fake_worktree), "branch": "feat/issue-99"}],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 1
    mock_release.assert_awaited_once_with(
        worktree_path=str(fake_worktree),
        repo_dir="/app",
    )


@pytest.mark.anyio
async def test_reaper_clears_db_ref_for_missing_dirs(tmp_path: Path) -> None:
    """Reaper clears the DB ref when the directory is already gone.

    Regression for the stale-loop bug: when a worktree directory no longer
    exists on disk, the old code did `continue` without clearing the DB.
    get_terminal_runs_with_worktrees() would then return the same run on every
    subsequent pass, spamming the log indefinitely.
    """
    absent = str(tmp_path / "issue-100")  # directory not created

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{"id": "issue-100", "worktree_path": absent, "branch": "feat/issue-100"}],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    # Directory already gone — release_worktree is never called, but the DB
    # reference IS cleared so the run never reappears in future passes.
    assert count == 0
    mock_release.assert_not_called()
    mock_clear.assert_awaited_once_with("issue-100")


@pytest.mark.anyio
async def test_reaper_returns_zero_when_no_terminal_runs() -> None:
    """Reaper returns 0 and does nothing when there are no terminal runs."""
    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 0
    mock_release.assert_not_called()


@pytest.mark.anyio
async def test_reaper_counts_multiple_released_dirs(tmp_path: Path) -> None:
    """Reaper processes all stale worktrees and returns correct count."""
    dir_a = tmp_path / "issue-1"
    dir_b = tmp_path / "issue-2"
    dir_a.mkdir()
    dir_b.mkdir()
    absent = str(tmp_path / "issue-3")  # not on disk — must be skipped

    runs = [
        {"id": "issue-1", "worktree_path": str(dir_a), "branch": "feat/issue-1"},
        {"id": "issue-2", "worktree_path": str(dir_b), "branch": "feat/issue-2"},
        {"id": "issue-3", "worktree_path": absent, "branch": "feat/issue-3"},
    ]

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=runs,
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 2
    assert mock_release.await_count == 2


@pytest.mark.anyio
async def test_reaper_clears_db_only_when_release_succeeds(tmp_path: Path) -> None:
    """Reaper clears worktree_path in DB only after release_worktree returns True."""
    fake_worktree = tmp_path / "issue-101"
    fake_worktree.mkdir()

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{"id": "issue-101", "worktree_path": str(fake_worktree), "branch": "feat/issue-101"}],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 1
    mock_clear.assert_awaited_once_with("issue-101")


@pytest.mark.anyio
async def test_reaper_does_not_clear_db_when_release_fails(tmp_path: Path) -> None:
    """Reaper does not clear worktree_path when release_worktree returns False."""
    fake_worktree = tmp_path / "issue-102"
    fake_worktree.mkdir()

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{"id": "issue-102", "worktree_path": str(fake_worktree), "branch": "feat/issue-102"}],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 0
    mock_clear.assert_not_called()


# ── release_worktree fallback tests ───────────────────────────────────────────


@pytest.mark.anyio
async def test_release_worktree_falls_back_to_rmtree_when_git_rejects(tmp_path: Path) -> None:
    """release_worktree uses shutil.rmtree when git worktree remove fails.

    Regression for the 'is not a working tree' loop: after a container restart
    git's worktree registry is reset, so git worktree remove --force fails even
    though the directory exists.  The old code returned False, the reaper never
    cleared the DB, and the same run appeared in every subsequent pass.
    """
    stale_dir = tmp_path / "issue-824"
    stale_dir.mkdir()

    import asyncio

    async def fake_run(*args: object, **kwargs: object) -> object:  # noqa: ARG001
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a working tree"))
        return proc

    with (
        patch("agentception.services.teardown.asyncio.create_subprocess_exec", side_effect=fake_run),
        patch("agentception.services.teardown.shutil.rmtree") as mock_rmtree,
    ):
        from agentception.services.teardown import release_worktree

        result = await release_worktree(
            worktree_path=str(stale_dir),
            repo_dir=str(tmp_path),
        )

    assert result is True
    mock_rmtree.assert_called_once_with(str(stale_dir))


@pytest.mark.anyio
async def test_release_worktree_returns_false_when_both_git_and_rmtree_fail(tmp_path: Path) -> None:
    """release_worktree returns False only when both git and shutil.rmtree fail."""
    stale_dir = tmp_path / "issue-825"
    stale_dir.mkdir()

    async def fake_run(*args: object, **kwargs: object) -> object:  # noqa: ARG001
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a working tree"))
        return proc

    with (
        patch("agentception.services.teardown.asyncio.create_subprocess_exec", side_effect=fake_run),
        patch(
            "agentception.services.teardown.shutil.rmtree",
            side_effect=OSError("permission denied"),
        ),
    ):
        from agentception.services.teardown import release_worktree

        result = await release_worktree(
            worktree_path=str(stale_dir),
            repo_dir=str(tmp_path),
        )

    assert result is False


# ── Label-run branch deletion ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_reaper_deletes_branch_for_label_run(tmp_path: Path) -> None:
    """Reaper deletes remote and local branches for stale label-* runs.

    Label/coordinator agents use agent/<slug> branches that never back a PR,
    so the reaper is safe (and responsible) for cleaning them up.
    """
    fake_worktree = tmp_path / "label-feature-abc123"
    fake_worktree.mkdir()

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{
                "id": "label-feature-abc123",
                "worktree_path": str(fake_worktree),
                "branch": "agent/feature",
            }],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.services.worktree_reaper._delete_label_branch",
            new_callable=AsyncMock,
        ) as mock_del_branch,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 1
    mock_del_branch.assert_awaited_once_with("agent/feature", "/app")


@pytest.mark.anyio
async def test_reaper_does_not_delete_branch_for_issue_run(tmp_path: Path) -> None:
    """Reaper does NOT delete branches for issue-* runs (they may have open PRs)."""
    fake_worktree = tmp_path / "issue-999"
    fake_worktree.mkdir()

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{
                "id": "issue-999",
                "worktree_path": str(fake_worktree),
                "branch": "feat/issue-999",
            }],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.services.worktree_reaper._delete_label_branch",
            new_callable=AsyncMock,
        ) as mock_del_branch,
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 1
    mock_del_branch.assert_not_called()


@pytest.mark.anyio
async def test_reaper_deletes_label_branch_for_already_gone_dir(tmp_path: Path) -> None:
    """Reaper deletes label branch even when the worktree dir is already gone."""
    absent = str(tmp_path / "label-gone-abc")

    with (
        patch(
            "agentception.services.worktree_reaper.get_terminal_runs_with_worktrees",
            new_callable=AsyncMock,
            return_value=[{
                "id": "label-gone-abc",
                "worktree_path": absent,
                "branch": "agent/gone",
            }],
        ),
        patch(
            "agentception.services.worktree_reaper.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.services.worktree_reaper.clear_run_worktree_path",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "agentception.services.worktree_reaper._delete_label_branch",
            new_callable=AsyncMock,
        ) as mock_del_branch,
        patch(
            "agentception.services.worktree_reaper._prune_worktree_refs",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.services.worktree_reaper.settings",
            new_callable=MagicMock,
            repo_dir="/app",
        ),
    ):
        from agentception.services.worktree_reaper import reap_stale_worktrees

        count = await reap_stale_worktrees()

    assert count == 0  # dir was already gone, reaped count is 0
    mock_release.assert_not_called()
    mock_clear.assert_awaited_once_with("label-gone-abc")
    mock_del_branch.assert_awaited_once_with("agent/gone", "/app")


# ── _reaper_loop exception guard ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_reaper_loop_continues_after_exception() -> None:
    """_reaper_loop survives an unhandled exception and continues on the next tick.

    Regression: without the try/except wrapper a single DB error at 3 AM
    would kill the asyncio task permanently, leaving orphaned worktrees until
    the next container restart.

    The sleep mock uses the real asyncio.sleep(0) to actually yield to the
    event loop so the background task makes progress.  On the 3rd sleep call
    it raises CancelledError to terminate the loop gracefully.
    """
    import asyncio as _asyncio

    # Capture the real sleep before any patching so yielding still works.
    _real_sleep = _asyncio.sleep

    call_count = 0
    sleep_calls = 0

    async def flaky_reap() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient DB error")
        return 0

    async def counting_sleep(_seconds: float) -> None:
        """Yield to the real event loop, then cancel after 3 calls (= 2 reap iterations)."""
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise _asyncio.CancelledError()
        await _real_sleep(0)  # real yield so the task actually progresses

    with (
        patch("agentception.app.reap_stale_worktrees", side_effect=flaky_reap),
        patch("agentception.app.asyncio.sleep", side_effect=counting_sleep),
    ):
        from agentception.app import _reaper_loop

        task = _asyncio.create_task(_reaper_loop())
        try:
            await task
        except _asyncio.CancelledError:
            pass

    # Both iterations ran — the exception in the first did not kill the loop.
    assert call_count >= 2
