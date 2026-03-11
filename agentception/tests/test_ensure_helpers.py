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


@pytest.mark.anyio
async def test_ensure_worktree_reset_removes_stale_dir_and_branch(tmp_path: Path) -> None:
    """ensure_worktree with reset=True tears down any existing dir/branch before recreating."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir(parents=True)
    branch = "feat/issue-123"
    base_ref = "origin/dev"

    success_proc = AsyncMock()
    success_proc.returncode = 0
    success_proc.communicate.return_value = (b"", b"")

    calls: list[list[str]] = []

    async def capture_proc(*args: str, **kwargs: object) -> AsyncMock:
        calls.append(list(args))
        return success_proc

    with (
        patch("agentception.readers.git._git", new_callable=AsyncMock, return_value="  feat/issue-123"),
        patch("agentception.readers.git.asyncio.create_subprocess_exec", side_effect=capture_proc),
        patch("agentception.readers.git.shutil.rmtree"),
    ):
        result = await ensure_worktree(worktree_path, branch, base_ref, reset=True)

    assert result is True
    # git calls are: ("git", "-C", repo, verb, subcommand, ...)
    # Extract (verb, subcommand) pairs — indices 3 and 4.
    cmd_verbs = [tuple(c[3:5]) for c in calls if len(c) >= 5]
    assert ("worktree", "remove") in cmd_verbs, f"Expected worktree remove in {cmd_verbs}"
    assert ("branch", "-D") in cmd_verbs, f"Expected branch -D in {cmd_verbs}"
    assert ("worktree", "add") in cmd_verbs, f"Expected worktree add in {cmd_verbs}"
    # Remote branch must also be deleted so subsequent pushes never pick up stale commits.
    assert ("push", "origin") in cmd_verbs, f"Expected 'git push origin --delete' in {cmd_verbs}"


@pytest.mark.anyio
async def test_ensure_worktree_reset_deletes_remote_branch_stale_state(tmp_path: Path) -> None:
    """ensure_worktree reset=True deletes the remote branch before recreating.

    Regression test: without this, a re-dispatched executor pushes on top of
    the previous run's remote branch, giving the new worktree stale commits from
    the prior run on the first git pull / checkout.
    """
    worktree_path = tmp_path / "issue-449"
    worktree_path.mkdir(parents=True)
    branch = "feat/issue-449"
    base_ref = "origin/dev"

    success_proc = AsyncMock()
    success_proc.returncode = 0
    success_proc.communicate.return_value = (b"", b"")

    push_delete_calls: list[list[str]] = []

    async def capture_proc(*args: str, **kwargs: object) -> AsyncMock:
        if "push" in args and "--delete" in args:
            push_delete_calls.append(list(args))
        return success_proc

    with (
        patch("agentception.readers.git._git", new_callable=AsyncMock, return_value="  feat/issue-449"),
        patch("agentception.readers.git.asyncio.create_subprocess_exec", side_effect=capture_proc),
        patch("agentception.readers.git.shutil.rmtree"),
    ):
        await ensure_worktree(worktree_path, branch, base_ref, reset=True)

    assert len(push_delete_calls) == 1, (
        f"Expected exactly one 'git push origin --delete' call, got: {push_delete_calls}"
    )
    assert "--delete" in push_delete_calls[0], "Remote branch deletion must use --delete flag"
    assert branch in push_delete_calls[0], f"Must delete branch {branch!r}, got: {push_delete_calls[0]}"


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

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
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
            role="reviewer",
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

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
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


@pytest.mark.anyio
async def test_dispatch_reviewer_pr_branch_override_respected(tmp_path: Path) -> None:
    """When pr_branch is provided, it overrides the feat/issue-{N} default branch name.

    This covers PRs whose branch doesn't follow the standard naming convention
    (e.g. feat/reviewer-branch-orientation vs feat/issue-35).
    """
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    fetch_proc = AsyncMock()
    fetch_proc.returncode = 0
    fetch_proc.communicate.return_value = (b"", b"")

    captured_bases: list[str] = []
    captured_branches: list[str] = []

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
        captured_bases.append(base)
        captured_branches.append(branch)
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
            issue_title="PR review for non-standard branch",
            issue_body="",
            role="reviewer",
            repo="agentception",
            pr_number=437,
            pr_branch="feat/reviewer-branch-orientation",
        )
        await dispatch_agent(req)

    # The custom pr_branch must be fetched and used as the worktree base
    assert captured_branches == ["feat/reviewer-branch-orientation"], (
        f"Expected branch 'feat/reviewer-branch-orientation', got {captured_branches}"
    )
    assert captured_bases == ["origin/feat/reviewer-branch-orientation"], (
        f"Expected base 'origin/feat/reviewer-branch-orientation', got {captured_bases}"
    )


@pytest.mark.anyio
async def test_dispatch_reviewer_deleted_branch_returns_422(tmp_path: Path) -> None:
    """When the remote branch is gone (already merged + deleted), dispatch returns 422 not 500."""
    from fastapi import HTTPException
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    # Simulate git fetch failing because the branch was deleted after merge
    fetch_proc = AsyncMock()
    fetch_proc.returncode = 128
    fetch_proc.communicate.return_value = (
        b"",
        b"fatal: couldn't find remote ref feat/issue-35",
    )

    with (
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", return_value=fetch_proc),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.host_worktrees_dir = str(tmp_path / "host_worktrees")
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = DispatchRequest(
            issue_number=35,
            issue_title="PR review for already-merged PR",
            issue_body="",
            role="reviewer",
            repo="agentception",
            pr_number=436,
        )
        with pytest.raises(HTTPException) as exc_info:
            await dispatch_agent(req)

    assert exc_info.value.status_code == 422
    assert "already merged" in exc_info.value.detail or "pr_branch" in exc_info.value.detail


@pytest.mark.anyio
async def test_dispatch_resets_stale_working_memory_on_redispatch(tmp_path: Path) -> None:
    """dispatch_agent overwrites memory.json so a re-dispatched run does not inherit
    stale context from a prior run sharing the same run_id.

    Regression test for: agent woke up with loop-guard memory when dispatched for
    stall-detection work because the issue-33 worktree was reused and its old
    memory.json was read on turn 1.
    """
    import json
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest
    from agentception.services.working_memory import WorkingMemory, write_memory

    # Simulate a worktree that already exists with stale memory from a prior run.
    worktree_path = tmp_path / "worktrees" / "issue-99"
    worktree_path.mkdir(parents=True)
    stale = WorkingMemory(
        plan="Implement loop guard detection (old task)",
        findings={"agentception/poller.py": "loop guard is already implemented"},
    )
    write_memory(worktree_path, stale)

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
        return True  # worktree "already exists" — no-op

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
            issue_number=99,
            issue_title="New task: stall detection",
            issue_body="Implement two-signal stall detection.",
            role="developer",
            repo="agentception",
        )
        await dispatch_agent(req)

    # The memory file must be rewritten with the new task's plan, not the old one.
    memory_file = worktree_path / ".agentception" / "memory.json"
    assert memory_file.exists(), "dispatch_agent must write memory.json into the worktree"
    raw = json.loads(memory_file.read_text())
    assert "loop guard" not in raw.get("plan", ""), (
        "Stale loop-guard plan must not survive a re-dispatch"
    )
    assert "stall" in raw.get("plan", "").lower(), (
        "New plan must be seeded from the new task_description"
    )
    # Stale findings from the old run must be gone.
    assert raw.get("findings") in (None, {}), (
        "Stale findings from prior run must be cleared on re-dispatch"
    )


# ---------------------------------------------------------------------------
# _ast_signatures_from_file
# ---------------------------------------------------------------------------


def test_ast_signatures_extracts_class_and_function(tmp_path: Path) -> None:
    """_ast_signatures_from_file returns class and function declaration lines."""
    from agentception.routes.api.dispatch import _ast_signatures_from_file

    src = tmp_path / "mymodule.py"
    src.write_text(
        "class Foo(BaseModel):\n"
        "    x: int\n"
        "\n"
        "def bar(a: int, b: str) -> bool:\n"
        "    return True\n",
        encoding="utf-8",
    )
    result = _ast_signatures_from_file(src)
    assert "class Foo" in result
    assert "def bar" in result


def test_ast_signatures_returns_empty_on_syntax_error(tmp_path: Path) -> None:
    """_ast_signatures_from_file returns empty string for unparseable files."""
    from agentception.routes.api.dispatch import _ast_signatures_from_file

    bad = tmp_path / "bad.py"
    bad.write_text("def (: broken syntax!!!!", encoding="utf-8")
    assert _ast_signatures_from_file(bad) == ""


def test_ast_signatures_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """_ast_signatures_from_file returns empty string when the file doesn't exist."""
    from agentception.routes.api.dispatch import _ast_signatures_from_file

    assert _ast_signatures_from_file(tmp_path / "nonexistent.py") == ""


# ---------------------------------------------------------------------------
# _extract_type_signatures (async wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_extract_type_signatures_returns_signatures_for_py_files(tmp_path: Path) -> None:
    """_extract_type_signatures returns a dict entry for each readable Python file."""
    from agentception.routes.api.dispatch import _extract_type_signatures

    (tmp_path / "agentception").mkdir()
    src = tmp_path / "agentception" / "models.py"
    src.write_text("class MyModel:\n    pass\n", encoding="utf-8")

    result = await _extract_type_signatures(tmp_path, ["agentception/models.py"])
    assert "agentception/models.py" in result
    assert "MyModel" in result["agentception/models.py"]


@pytest.mark.anyio
async def test_extract_type_signatures_skips_non_python_files(tmp_path: Path) -> None:
    """_extract_type_signatures silently skips non-.py files."""
    from agentception.routes.api.dispatch import _extract_type_signatures

    result = await _extract_type_signatures(tmp_path, ["agentception/overview.html"])
    assert result == {}


# ---------------------------------------------------------------------------
# _test_names_from_file / _extract_test_coverage
# ---------------------------------------------------------------------------


def test_test_names_from_file_finds_test_functions(tmp_path: Path) -> None:
    """_test_names_from_file returns only def test_* names."""
    from agentception.routes.api.dispatch import _test_names_from_file

    src = tmp_path / "test_mymodule.py"
    src.write_text(
        "def helper(): pass\n"
        "def test_foo(): pass\n"
        "async def test_bar(): pass\n",
        encoding="utf-8",
    )
    names = _test_names_from_file(src)
    assert names == ["test_foo", "test_bar"]
    assert "helper" not in names


@pytest.mark.anyio
async def test_extract_test_coverage_matches_source_to_test_file(tmp_path: Path) -> None:
    """_extract_test_coverage finds test_agentception_poller.py for agentception/poller.py."""
    from agentception.routes.api.dispatch import _extract_test_coverage

    tests_dir = tmp_path / "agentception" / "tests"
    tests_dir.mkdir(parents=True)
    test_file = tests_dir / "test_agentception_poller.py"
    test_file.write_text(
        "def test_stall_detected(): pass\n"
        "def test_no_stall_when_recent(): pass\n",
        encoding="utf-8",
    )

    result = await _extract_test_coverage(tmp_path, ["agentception/poller.py"])
    key = "agentception/tests/test_agentception_poller.py"
    assert key in result
    assert "test_stall_detected" in result[key]
    assert "test_no_stall_when_recent" in result[key]


# ---------------------------------------------------------------------------
# _extract_ac_items
# ---------------------------------------------------------------------------


def test_extract_ac_items_returns_empty_when_no_ac_section() -> None:
    """_extract_ac_items returns [] when the issue body has no AC section."""
    from agentception.routes.api.dispatch import _extract_ac_items

    body = "## Overview\n\nFix the bug.\n\n## Notes\n\n- [ ] Note item"
    assert _extract_ac_items(body) == []


def test_extract_ac_items_extracts_checkbox_bullets() -> None:
    """_extract_ac_items returns each checkbox bullet prefixed with 'AC:'."""
    from agentception.routes.api.dispatch import _extract_ac_items

    body = (
        "## Acceptance criteria\n\n"
        "- [ ] Add `file_hash` field to `_ChunkSpec`\n"
        "- [ ] Delete stale chunks on re-index\n"
        "- [x] Already done item\n"
    )
    items = _extract_ac_items(body)
    assert items == [
        "AC: Add `file_hash` field to `_ChunkSpec`",
        "AC: Delete stale chunks on re-index",
        "AC: Already done item",
    ]


def test_extract_ac_items_stops_at_next_section() -> None:
    """_extract_ac_items does not bleed past the next Markdown section header."""
    from agentception.routes.api.dispatch import _extract_ac_items

    body = (
        "## Acceptance criteria\n\n"
        "- [ ] Item A\n"
        "- [ ] Item B\n"
        "\n"
        "## Out of scope\n\n"
        "- [ ] Should NOT be included\n"
    )
    items = _extract_ac_items(body)
    assert items == ["AC: Item A", "AC: Item B"]
    assert "Should NOT be included" not in " ".join(items)


def test_extract_ac_items_case_insensitive_header() -> None:
    """_extract_ac_items matches 'Acceptance Criteria' regardless of capitalisation."""
    from agentception.routes.api.dispatch import _extract_ac_items

    body = "### Acceptance Criteria\n\n- [ ] Case-insensitive match\n"
    items = _extract_ac_items(body)
    assert items == ["AC: Case-insensitive match"]


def test_extract_ac_items_returns_empty_for_empty_body() -> None:
    """_extract_ac_items handles an empty string without error."""
    from agentception.routes.api.dispatch import _extract_ac_items

    assert _extract_ac_items("") == []


# ---------------------------------------------------------------------------
# dispatch_agent — AC injection into next_steps
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dispatch_agent_seeds_next_steps_from_ac_items(tmp_path: Path) -> None:
    """dispatch_agent pre-populates next_steps with verbatim AC bullets from the issue body.

    Verifies the structural fix for the lossy-reading problem: the agent must
    start iteration 1 with every AC item already in next_steps so it cannot
    paraphrase, collapse, or drop any requirement.
    """
    import json
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    worktree_path = tmp_path / "worktrees" / "issue-77"
    worktree_path.mkdir(parents=True)

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
        return True

    issue_body = (
        "## Summary\n\nAdd incremental indexing.\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] Add `file_hash` to `_ChunkSpec`\n"
        "- [ ] Skip unchanged files\n"
        "- [ ] Delete chunks for removed files\n"
        "\n"
        "## Notes\n\n"
        "- [ ] Not an AC item\n"
    )

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
            issue_number=77,
            issue_title="Add incremental indexing",
            issue_body=issue_body,
            role="developer",
            repo="agentception",
        )
        await dispatch_agent(req)

    memory_file = worktree_path / ".agentception" / "memory.json"
    assert memory_file.exists(), "dispatch_agent must write memory.json"
    raw = json.loads(memory_file.read_text())
    next_steps: list[str] = raw.get("next_steps", [])
    assert next_steps == [
        "AC: Add `file_hash` to `_ChunkSpec`",
        "AC: Skip unchanged files",
        "AC: Delete chunks for removed files",
    ], f"Expected AC items in next_steps, got: {next_steps}"
    assert "AC: Not an AC item" not in next_steps, (
        "Items from non-AC sections must not leak into next_steps"
    )


@pytest.mark.anyio
async def test_dispatch_agent_reviewer_does_not_seed_ac_items(tmp_path: Path) -> None:
    """PR-reviewer dispatch must NOT pre-populate next_steps with AC items.

    The reviewer's working memory is seeded with the review task description,
    not implementation checkboxes.
    """
    import json
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    # Reviewer dispatch uses slug "review-{pr_number}", not "issue-{N}".
    worktree_path = tmp_path / "worktrees" / "review-500"
    worktree_path.mkdir(parents=True)

    fetch_proc = MagicMock()
    fetch_proc.returncode = 0
    fetch_proc.communicate = AsyncMock(return_value=(b"", b""))

    async def _fake_subprocess(*args: object, **kwargs: object) -> MagicMock:
        return fetch_proc

    issue_body = (
        "## Acceptance criteria\n\n"
        "- [ ] Item that belongs to the developer, not the reviewer\n"
    )

    with (
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", side_effect=_fake_subprocess),
        patch("agentception.readers.git.ensure_worktree", new_callable=AsyncMock, return_value=True),
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
            issue_number=88,
            issue_title="My Feature",
            issue_body=issue_body,
            role="reviewer",
            repo="agentception",
            pr_number=500,
            pr_branch="feat/issue-88",
        )
        await dispatch_agent(req)

    memory_file = worktree_path / ".agentception" / "memory.json"
    assert memory_file.exists()
    raw = json.loads(memory_file.read_text())
    assert raw.get("next_steps", []) == [], (
        "Reviewer dispatch must not seed AC items into next_steps"
    )
