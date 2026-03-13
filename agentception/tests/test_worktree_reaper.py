from __future__ import annotations

"""Tests for the worktree reaper.

Critical invariant: the reaper must call release_worktree (dir removal only),
never teardown_agent_worktree (which deletes remote branches and closes open PRs).
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
async def test_reaper_skips_missing_dirs(tmp_path: Path) -> None:
    """Reaper skips runs whose worktree directory no longer exists on disk."""
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
