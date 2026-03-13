"""Tests for prompt_variant pass-through in the dispatch endpoint."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_dispatch_passes_prompt_variant_to_task_spec(tmp_path: Path) -> None:
    """POST /api/dispatch/issue with prompt_variant passes it to persist_agent_run_dispatch."""
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    captured_kwargs: list[dict[str, Any]] = []

    async def mock_persist(**kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.append(kwargs)

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
        return True

    with (
        patch("agentception.readers.git.ensure_worktree", side_effect=mock_ensure_worktree),
        patch("agentception.routes.api.dispatch._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.search_codebase", new_callable=AsyncMock, return_value=[]),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", side_effect=mock_persist),
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
            issue_number=1,
            issue_title="Test issue",
            issue_body="",
            role="developer",
            repo="agentception",
            prompt_variant="streamlined",
        )
        await dispatch_agent(req)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["prompt_variant"] == "streamlined"


@pytest.mark.anyio
async def test_dispatch_prompt_variant_defaults_to_none(tmp_path: Path) -> None:
    """POST /api/dispatch/issue without prompt_variant passes None to persist_agent_run_dispatch."""
    from agentception.routes.api.dispatch import dispatch_agent, DispatchRequest

    captured_kwargs: list[dict[str, Any]] = []

    async def mock_persist(**kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.append(kwargs)

    async def mock_ensure_worktree(path: Path, branch: str, base: str = "origin/dev", reset: bool = False) -> bool:
        return True

    with (
        patch("agentception.readers.git.ensure_worktree", side_effect=mock_ensure_worktree),
        patch("agentception.routes.api.dispatch._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.search_codebase", new_callable=AsyncMock, return_value=[]),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", side_effect=mock_persist),
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
            issue_number=2,
            issue_title="Test issue without variant",
            issue_body="",
            role="developer",
            repo="agentception",
        )
        await dispatch_agent(req)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["prompt_variant"] is None
