"""Tests for cognitive architecture propagation through agent spawn chains.

Verifies that ``spawn_child()`` forwards the parent's ``cognitive_arch``
unchanged rather than re-resolving it, and that the propagation works across
a multi-tier tree (root coordinator → child coordinator → leaf).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.services.spawn_child import SpawnChildResult, spawn_child


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_result(cognitive_arch: str) -> SpawnChildResult:
    """Build a minimal SpawnChildResult for testing propagation assertions."""
    return SpawnChildResult(
        run_id="test-run-id",
        host_worktree_path="/host/worktrees/test-run-id",
        worktree_path="/worktrees/test-run-id",
        tier="coordinator",
        org_domain="engineering",
        role="engineering-coordinator",
        cognitive_arch=cognitive_arch,
        scope_type="label",
        scope_value="my-label",
    )


# ---------------------------------------------------------------------------
# Unit tests — spawn_child cognitive_arch parameter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_child_forwards_cognitive_arch_without_resolving() -> None:
    """When cognitive_arch is provided, _resolve_cognitive_arch must NOT be called."""
    parent_arch = "feynman:llm:python"

    with (
        patch("agentception.services.spawn_child._resolve_cognitive_arch") as mock_resolve,
        patch("agentception.services.spawn_child.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch("agentception.services.spawn_child.acknowledge_agent_run", new_callable=AsyncMock),
        patch("agentception.services.spawn_child._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.services.spawn_child._index_worktree", new_callable=AsyncMock),
        patch("agentception.services.spawn_child.settings") as mock_settings,
        patch("pathlib.Path.write_text"),
        patch("pathlib.Path.exists", return_value=True),
    ):
        mock_settings.worktrees_dir = MagicMock()
        mock_settings.worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/worktrees/{x}")
        mock_settings.host_worktrees_dir = MagicMock()
        mock_settings.host_worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/host/worktrees/{x}")
        mock_settings.repo_dir = "/repo"
        mock_settings.ac_url = "http://localhost:1337"

        # Mock git commands — rev-parse and worktree add both succeed
        git_proc = AsyncMock()
        git_proc.returncode = 0
        git_proc.communicate = AsyncMock(return_value=(b"abc123def456\n", b""))
        wt_proc = AsyncMock()
        wt_proc.returncode = 0
        wt_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec.side_effect = [git_proc, wt_proc]

        result = await spawn_child(
            parent_run_id="coord-root-abc",
            role="engineering-coordinator",
            tier="coordinator",
            scope_type="label",
            scope_value="my-label",
            gh_repo="owner/repo",
            cognitive_arch=parent_arch,
        )

    mock_resolve.assert_not_called()
    assert result.cognitive_arch == parent_arch


@pytest.mark.anyio
async def test_spawn_child_resolves_arch_when_not_provided() -> None:
    """When cognitive_arch is omitted, _resolve_cognitive_arch should be called."""
    resolved = "hopper:python"

    with (
        patch("agentception.services.spawn_child._resolve_cognitive_arch", return_value=resolved) as mock_resolve,
        patch("agentception.services.spawn_child.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch("agentception.services.spawn_child.acknowledge_agent_run", new_callable=AsyncMock),
        patch("agentception.services.spawn_child._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.services.spawn_child._index_worktree", new_callable=AsyncMock),
        patch("agentception.services.spawn_child.settings") as mock_settings,
        patch("pathlib.Path.write_text"),
        patch("pathlib.Path.exists", return_value=True),
    ):
        mock_settings.worktrees_dir = MagicMock()
        mock_settings.worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/worktrees/{x}")
        mock_settings.host_worktrees_dir = MagicMock()
        mock_settings.host_worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/host/worktrees/{x}")
        mock_settings.repo_dir = "/repo"
        mock_settings.ac_url = "http://localhost:1337"

        git_proc = AsyncMock()
        git_proc.returncode = 0
        git_proc.communicate = AsyncMock(return_value=(b"abc123def456\n", b""))
        wt_proc = AsyncMock()
        wt_proc.returncode = 0
        wt_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec.side_effect = [git_proc, wt_proc]

        result = await spawn_child(
            parent_run_id="coord-root-abc",
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="42",
            gh_repo="owner/repo",
        )

    mock_resolve.assert_called_once()
    assert result.cognitive_arch == resolved


# ---------------------------------------------------------------------------
# Integration-style test — two-level propagation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cognitive_arch_propagates_to_leaf() -> None:
    """Root coordinator arch must arrive unchanged on the leaf after two spawn calls.

    Simulates: root coordinator (arch A) spawns sub-coordinator (arch A forwarded)
    which then spawns a leaf engineer (arch A forwarded again).
    All infrastructure (git, DB) is mocked so this runs without side-effects.
    """
    root_arch = "feynman:llm:python"

    def _make_spawn_mock(expected_arch: str) -> AsyncMock:
        """Return an async mock for spawn_child that checks arch and returns a result."""
        mock = AsyncMock(return_value=_make_mock_result(expected_arch))
        return mock

    # --- Step 1: root coordinator spawns sub-coordinator, forwarding root_arch ---
    with patch("agentception.services.spawn_child._resolve_cognitive_arch") as mock_resolve_1, \
         patch("agentception.services.spawn_child.asyncio.create_subprocess_exec") as mock_exec_1, \
         patch("agentception.services.spawn_child.persist_agent_run_dispatch", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child.acknowledge_agent_run", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child._configure_worktree_auth", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child._index_worktree", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child.settings") as mock_settings_1, \
         patch("pathlib.Path.write_text"), \
         patch("pathlib.Path.exists", return_value=True):

        mock_settings_1.worktrees_dir = MagicMock()
        mock_settings_1.worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/worktrees/{x}")
        mock_settings_1.host_worktrees_dir = MagicMock()
        mock_settings_1.host_worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/host/worktrees/{x}")
        mock_settings_1.repo_dir = "/repo"
        mock_settings_1.ac_url = "http://localhost:1337"

        git_proc = AsyncMock()
        git_proc.returncode = 0
        git_proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))
        wt_proc = AsyncMock()
        wt_proc.returncode = 0
        wt_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec_1.side_effect = [git_proc, wt_proc]

        sub_coord_result = await spawn_child(
            parent_run_id="coord-root-xyz",
            role="engineering-coordinator",
            tier="coordinator",
            scope_type="label",
            scope_value="my-initiative",
            gh_repo="owner/repo",
            cognitive_arch=root_arch,  # root forwards its own arch
        )

    # _resolve_cognitive_arch must NOT have been called — arch was forwarded
    mock_resolve_1.assert_not_called()
    sub_coord_arch = sub_coord_result.cognitive_arch
    assert sub_coord_arch == root_arch, (
        f"Sub-coordinator arch {sub_coord_arch!r} != root arch {root_arch!r}"
    )

    # --- Step 2: sub-coordinator spawns leaf, forwarding the same arch ---
    with patch("agentception.services.spawn_child._resolve_cognitive_arch") as mock_resolve_2, \
         patch("agentception.services.spawn_child.asyncio.create_subprocess_exec") as mock_exec_2, \
         patch("agentception.services.spawn_child.persist_agent_run_dispatch", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child.acknowledge_agent_run", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child._configure_worktree_auth", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child._index_worktree", new_callable=AsyncMock), \
         patch("agentception.services.spawn_child.settings") as mock_settings_2, \
         patch("pathlib.Path.write_text"), \
         patch("pathlib.Path.exists", return_value=True):

        mock_settings_2.worktrees_dir = MagicMock()
        mock_settings_2.worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/worktrees/{x}")
        mock_settings_2.host_worktrees_dir = MagicMock()
        mock_settings_2.host_worktrees_dir.__truediv__ = lambda s, x: MagicMock(__str__=lambda _: f"/host/worktrees/{x}")
        mock_settings_2.repo_dir = "/repo"
        mock_settings_2.ac_url = "http://localhost:1337"

        git_proc_2 = AsyncMock()
        git_proc_2.returncode = 0
        git_proc_2.communicate = AsyncMock(return_value=(b"abc123\n", b""))
        wt_proc_2 = AsyncMock()
        wt_proc_2.returncode = 0
        wt_proc_2.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec_2.side_effect = [git_proc_2, wt_proc_2]

        leaf_result = await spawn_child(
            parent_run_id=sub_coord_result.run_id,
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="99",
            gh_repo="owner/repo",
            cognitive_arch=sub_coord_arch,  # sub-coordinator forwards its arch
        )

    # Again, _resolve_cognitive_arch must NOT have been called
    mock_resolve_2.assert_not_called()
    leaf_arch = leaf_result.cognitive_arch
    assert leaf_arch == root_arch, (
        f"Leaf arch {leaf_arch!r} != root arch {root_arch!r} — propagation broken"
    )
