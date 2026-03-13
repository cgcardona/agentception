"""Unit tests for agentception.services.run_factory._configure_worktree_auth."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _make_proc(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    """Return a mock subprocess whose communicate() resolves to (b'', stderr)."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.anyio
async def test_configure_worktree_auth_enables_worktree_config() -> None:
    """Happy path: exactly one git config call enables extensions.worktreeConfig.

    Auth is handled by the container-wide askpass helper (baked into the image
    via ``git config --system core.askPass``).  _configure_worktree_auth must
    not set any Authorization header or touch remote.origin.url — both risks
    triggering GitHub secret scanning and PAT revocation.
    """
    from agentception.services.run_factory import _configure_worktree_auth

    ext_proc = _make_proc()
    call_args: list[tuple[object, ...]] = []

    async def fake_exec(*args: object, **_kwargs: object) -> AsyncMock:
        call_args.append(args)
        return ext_proc

    with patch("agentception.services.run_factory.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")

    # Must have made exactly one git config call.
    assert len(call_args) == 1

    # That call must enable extensions.worktreeConfig only.
    assert call_args[0] == ("git", "config", "--local", "extensions.worktreeConfig", "true")

    # Sanity-check: no token-related calls were made.
    for args in call_args:
        assert "extraHeader" not in args, "Bearer extraHeader must not be set — git protocol needs Basic auth"
        assert "set-url" not in args, "remote.origin.url must never be modified"
        assert "Bearer" not in args, "Bearer tokens are rejected by GitHub's git protocol"


@pytest.mark.anyio
async def test_configure_worktree_auth_worktree_config_failure_logs_warning() -> None:
    """extensions.worktreeConfig git config fails → warning logged, no exception raised."""
    from agentception.services.run_factory import _configure_worktree_auth

    ext_proc = _make_proc(returncode=1, stderr=b"error: not in a git repo")

    async def fake_exec(*args: object, **_kwargs: object) -> AsyncMock:
        return ext_proc

    with patch("agentception.services.run_factory.asyncio.create_subprocess_exec", side_effect=fake_exec):
        # Must not raise — failure is logged as a warning only.
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")


@pytest.mark.anyio
async def test_configure_worktree_auth_no_bearer_header_ever_set() -> None:
    """Regression: Bearer extraHeader must never appear regardless of token presence.

    GitHub's git-receive-pack endpoint only accepts Basic auth.  The old
    implementation set Authorization: Bearer which caused all pushes to fail
    with HTTP 401 even when the token was valid for the REST API.
    """
    from agentception.services.run_factory import _configure_worktree_auth

    ext_proc = _make_proc()
    all_args: list[tuple[object, ...]] = []

    async def fake_exec(*args: object, **_kwargs: object) -> AsyncMock:
        all_args.append(args)
        return ext_proc

    with (
        patch("agentception.services.run_factory.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_testtoken123"}, clear=False),
    ):
        await _configure_worktree_auth(Path("/worktrees/issue-99"), "issue-99")

    # No git call should ever include a Bearer token or Authorization header.
    for args in all_args:
        flat = " ".join(str(a) for a in args)
        assert "Bearer" not in flat
        assert "Authorization" not in flat
        assert "extraHeader" not in flat
        assert "ghp_testtoken123" not in flat
