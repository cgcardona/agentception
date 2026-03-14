from __future__ import annotations

"""Tests for the worktree reaper.

Critical invariants:
- The reaper must call release_worktree (dir removal only), never
  teardown_agent_worktree (which deletes remote branches and closes open PRs).
- When a worktree directory is already gone, the reaper must clear the DB ref
  so the run never appears in future reaper passes (the stale-loop bug).
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
