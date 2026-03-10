"""Tests for idempotent ensure_* helpers in readers.git and readers.github."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agentception.readers.git import ensure_branch, ensure_worktree
from agentception.readers.github import ensure_pull_request


# ---------------------------------------------------------------------------
# ensure_worktree
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ensure_worktree_creates_new_worktree(tmp_path: Path) -> None:
    """ensure_worktree creates a new worktree when it does not exist."""
    worktree_path = tmp_path / "issue-123"
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")

    with patch("agentception.readers.git.asyncio.create_subprocess_exec", return_value=mock_proc):
        await ensure_worktree(worktree_path, branch, base_ref)

    # Verify git worktree add was called
    assert mock_proc.communicate.called


@pytest.mark.anyio
async def test_ensure_worktree_idempotent_when_exists(tmp_path: Path) -> None:
    """ensure_worktree returns immediately when worktree already exists."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir(parents=True)
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    mock_proc = AsyncMock()

    with patch("agentception.readers.git.asyncio.create_subprocess_exec", return_value=mock_proc):
        await ensure_worktree(worktree_path, branch, base_ref)

    # Verify git was NOT called
    assert not mock_proc.communicate.called


@pytest.mark.anyio
async def test_ensure_worktree_raises_on_git_failure(tmp_path: Path) -> None:
    """ensure_worktree raises RuntimeError when git worktree add fails."""
    worktree_path = tmp_path / "issue-123"
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    # Mock _git to return empty (branch doesn't exist)
    # Mock create_subprocess_exec to fail on worktree add
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"fatal: invalid reference")

    with (
        patch("agentception.readers.git._git", new_callable=AsyncMock, return_value=""),
        patch("agentception.readers.git.asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        with pytest.raises(RuntimeError, match="git worktree add failed"):
            await ensure_worktree(worktree_path, branch, base_ref)


# ---------------------------------------------------------------------------
# ensure_branch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ensure_branch_creates_new_branch() -> None:
    """ensure_branch creates a new branch when it does not exist."""
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    # Mock list_git_branches to return empty list (branch does not exist)
    # Mock create_subprocess_exec to succeed
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")

    with (
        patch("agentception.readers.git.list_git_branches", new_callable=AsyncMock, return_value=[]),
        patch("agentception.readers.git.asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        await ensure_branch(branch, base_ref)

    # Verify git branch was called
    assert mock_proc.communicate.called


@pytest.mark.anyio
async def test_ensure_branch_idempotent_when_exists() -> None:
    """ensure_branch returns immediately when branch already exists."""
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    # Mock _git to return the branch name (branch exists)
    with patch("agentception.readers.git._git", new_callable=AsyncMock, return_value=branch):
        created = await ensure_branch(branch, base_ref)

    # Verify branch was not created
    assert created is False


@pytest.mark.anyio
async def test_ensure_branch_raises_on_git_failure() -> None:
    """ensure_branch raises RuntimeError when git branch creation fails."""
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    # Mock _git to return empty (branch doesn't exist)
    # Mock create_subprocess_exec to fail on branch creation
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"fatal: not a valid object name")

    with (
        patch("agentception.readers.git._git", new_callable=AsyncMock, return_value=""),
        patch("agentception.readers.git.asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        with pytest.raises(RuntimeError, match="git branch .* failed"):
            await ensure_branch(branch, base_ref)


# ---------------------------------------------------------------------------
# ensure_pull_request
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ensure_pull_request_creates_new_pr() -> None:
    """ensure_pull_request creates a new PR when none exists."""
    head = "feat/issue-123"
    base = "dev"
    title = "Fix issue 123"
    body = "Closes #123"

    # Mock httpx.AsyncClient to return empty list (no PR exists), then success on create
    mock_get_response = MagicMock()
    mock_get_response.json.return_value = []
    mock_get_response.raise_for_status = MagicMock()

    mock_post_response = MagicMock()
    mock_post_response.json.return_value = {"number": 456}
    mock_post_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_get_response
    mock_client.__aenter__.return_value.post.return_value = mock_post_response

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock_client):
        pr_number, created = await ensure_pull_request(head, base, title, body)

    assert pr_number == 456
    assert created is True


@pytest.mark.anyio
async def test_ensure_pull_request_idempotent_when_exists() -> None:
    """ensure_pull_request returns existing PR when one already exists."""
    head = "feat/issue-123"
    base = "dev"
    title = "Fix issue 123"
    body = "Closes #123"

    # Mock httpx.AsyncClient to return an existing PR
    mock_get_response = MagicMock()
    mock_get_response.json.return_value = [{"number": 456}]
    mock_get_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_get_response

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock_client):
        pr_number, created = await ensure_pull_request(head, base, title, body)

    # Verify we got the existing PR
    assert pr_number == 456
    assert created is False


@pytest.mark.anyio
async def test_ensure_pull_request_raises_on_creation_failure() -> None:
    """ensure_pull_request raises RuntimeError when PR creation fails."""
    head = "feat/issue-123"
    base = "dev"
    title = "Fix issue 123"
    body = "Closes #123"

    # Mock httpx.AsyncClient to return empty list, then fail on create
    mock_get_response = MagicMock()
    mock_get_response.json.return_value = []
    mock_get_response.raise_for_status = MagicMock()

    mock_post_response = MagicMock()
    mock_post_response.status_code = 422
    mock_post_response.text = "Validation failed"
    mock_post_response.raise_for_status.side_effect = Exception("API error")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_get_response
    mock_client.__aenter__.return_value.post.return_value = mock_post_response

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(Exception):
            await ensure_pull_request(head, base, title, body)


# ---------------------------------------------------------------------------
# dispatch_agent — reviewer branch orientation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dispatch_reviewer_fetches_pr_branch_and_uses_it_as_base(tmp_path: Path) -> None:
    """PR-reviewer dispatch fetches the PR branch and passes origin/<branch> as base.

    The critical invariant: ensure_worktree is called with base="origin/feat/issue-35"
    (not "origin/dev") so the reviewer worktree starts on the implementer's code
    without any manual branch-switching turns.
    """
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    fetch_proc = AsyncMock()
    fetch_proc.returncode = 0
    fetch_proc.communicate.return_value = (b"", b"")

    captured_base: list[str] = []

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev") -> bool:
        captured_base.append(base)
        return True

    with (
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", return_value=fetch_proc),
        patch("agentception.readers.git.ensure_worktree", side_effect=mock_ensure_worktree),
        patch("agentception.routes.api.dispatch._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.search_codebase", new_callable=AsyncMock, return_value=[]),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.acknowledge_agent_run", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.run_agent_loop", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.asyncio.create_task", return_value=asyncio.Future()),
        patch("agentception.routes.api.dispatch._index_worktree", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.host_worktrees_dir = str(tmp_path / "host_worktrees")
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = DispatchRequest(
            issue_number=35,
            issue_title="PR review for feat/issue-35",
            issue_body="Review this PR.",
            role="pr-reviewer",
            repo="agentception",
            pr_number=436,
        )
        await dispatch_agent(req)

    # The worktree base must be the PR branch on origin, not origin/dev
    assert captured_base == ["origin/feat/issue-35"], (
        f"Expected ensure_worktree to be called with base='origin/feat/issue-35', got {captured_base}"
    )


@pytest.mark.anyio
async def test_dispatch_implementer_uses_origin_dev_as_base(tmp_path: Path) -> None:
    """Implementer dispatch uses origin/dev as the worktree base (no fetch step)."""
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    captured_base: list[str] = []

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev") -> bool:
        captured_base.append(base)
        return True

    with (
        patch("agentception.readers.git.ensure_worktree", side_effect=mock_ensure_worktree),
        patch("agentception.routes.api.dispatch._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.search_codebase", new_callable=AsyncMock, return_value=[]),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.acknowledge_agent_run", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.run_agent_loop", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.asyncio.create_task", return_value=asyncio.Future()),
        patch("agentception.routes.api.dispatch._index_worktree", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.host_worktrees_dir = str(tmp_path / "host_worktrees")
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = DispatchRequest(
            issue_number=42,
            issue_title="Implement some feature",
            issue_body="",
            role="developer",
            repo="agentception",
        )
        await dispatch_agent(req)

    assert captured_base == ["origin/dev"], (
        f"Expected ensure_worktree to be called with base='origin/dev', got {captured_base}"
    )
