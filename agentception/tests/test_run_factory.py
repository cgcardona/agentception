"""Unit tests for agentception.services.run_factory._configure_worktree_auth."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


def _make_proc(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    """Return a mock subprocess whose communicate() resolves to (b'', stderr)."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.anyio
async def test_configure_worktree_auth_sets_extraheader_not_remote_url() -> None:
    """Happy path: token present → extensions.worktreeConfig enabled, extraHeader written.

    Critically, remote.origin.url must never be touched — that was the old
    behaviour that polluted the shared .git/config and caused token revocation.
    """
    from agentception.services.run_factory import _configure_worktree_auth

    ext_proc = _make_proc()
    hdr_proc = _make_proc()

    call_args: list[tuple[object, ...]] = []

    async def fake_exec(*args: object, **_kwargs: object) -> AsyncMock:
        call_args.append(args)
        if "extensions.worktreeConfig" in args:
            return ext_proc
        return hdr_proc

    with (
        patch("agentception.services.run_factory.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_testtoken123"}, clear=False),
    ):
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")

    # Must have made exactly two git config calls.
    assert len(call_args) == 2

    # First call: enable extensions.worktreeConfig.
    assert call_args[0] == ("git", "config", "--local", "extensions.worktreeConfig", "true")

    # Second call: set extraHeader — NOT remote set-url.
    assert call_args[1] == (
        "git", "config", "--worktree",
        "http.https://github.com/.extraHeader",
        "Authorization: Bearer ghp_testtoken123",
    )

    # Sanity-check: remote.origin.url was never touched.
    for args in call_args:
        assert "set-url" not in args, "remote.origin.url must not be modified"


@pytest.mark.anyio
async def test_configure_worktree_auth_no_token_skips_git_calls() -> None:
    """No GITHUB_TOKEN → warning logged, no git subprocess spawned."""
    from agentception.services.run_factory import _configure_worktree_auth

    with (
        patch("agentception.services.run_factory.asyncio.create_subprocess_exec") as mock_exec,
        patch.dict("os.environ", {}, clear=False),
        patch("os.environ.get", return_value=""),
    ):
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")

    mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_configure_worktree_auth_extraheader_failure_logs_warning() -> None:
    """extraHeader git config fails → warning logged, no exception raised."""
    from agentception.services.run_factory import _configure_worktree_auth

    ext_proc = _make_proc(returncode=0)
    hdr_proc = _make_proc(returncode=1, stderr=b"error: not in a git repo")

    async def fake_exec(*args: object, **_kwargs: object) -> AsyncMock:
        if "extensions.worktreeConfig" in args:
            return ext_proc
        return hdr_proc

    with (
        patch("agentception.services.run_factory.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_testtoken123"}, clear=False),
    ):
        # Must not raise — failure is logged as a warning only.
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")
