from __future__ import annotations

"""Tests for the AgentCeption kill control-plane endpoint (AC-101).

Tests cover:
- 404 returned for an unknown worktree slug.
- Successful kill: worktree removal, agent:wip label cleared, prune called.
- Slug with no .agent-task file (no issue number) — still succeeds.

Run targeted:
    pytest agentception/tests/test_agentception_control.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tmp_worktree(tmp_path: Path) -> Path:
    """Return a temporary worktree directory with a populated .agent-task file."""
    worktree = tmp_path / "issue-999"
    worktree.mkdir()
    (worktree / ".agent-task").write_text(
        "WORKFLOW=issue-to-pr\nISSUE_NUMBER=999\nGH_REPO=cgcardona/maestro\n",
        encoding="utf-8",
    )
    return worktree


@pytest.fixture()
def tmp_worktree_no_task(tmp_path: Path) -> Path:
    """Return a temporary worktree directory WITHOUT an .agent-task file."""
    worktree = tmp_path / "pr-888"
    worktree.mkdir()
    return worktree


# ── 404 for unknown slug ──────────────────────────────────────────────────────


def test_kill_nonexistent_worktree_returns_404(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/control/kill/{slug} must return 404 when the worktree does not exist."""
    with patch("agentception.routes.control.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        mock_settings.repo_dir = tmp_path
        mock_settings.gh_repo = "cgcardona/maestro"

        response = client.post("/api/control/kill/nonexistent-slug")

    assert response.status_code == 404
    assert "nonexistent-slug" in response.json()["detail"]


# ── Successful kill ───────────────────────────────────────────────────────────


def test_kill_removes_worktree_and_clears_label(
    client: TestClient,
    tmp_worktree: Path,
) -> None:
    """A successful kill must call git worktree remove, gh issue edit, and git worktree prune."""
    slug = tmp_worktree.name  # "issue-999"
    parent = tmp_worktree.parent

    async def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    with (
        patch("agentception.routes.control.settings") as mock_settings,
        patch("agentception.routes.control._run", side_effect=fake_run) as mock_run,
    ):
        mock_settings.worktrees_dir = parent
        mock_settings.repo_dir = Path("/repo")
        mock_settings.gh_repo = "cgcardona/maestro"

        response = client.post(f"/api/control/kill/{slug}")

    assert response.status_code == 200
    assert response.json() == {"killed": slug}

    # Verify the three subprocess calls were made in order.
    calls = [tuple(call.args[0]) for call in mock_run.call_args_list]
    assert any("worktree" in c and "remove" in c for c in calls), (
        "git worktree remove must be called"
    )
    assert any("issue" in c and "edit" in c for c in calls), (
        "gh issue edit must be called to clear agent:wip"
    )
    assert any("worktree" in c and "prune" in c for c in calls), (
        "git worktree prune must be called"
    )


def test_kill_endpoint_requires_existing_slug(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """Killing a slug whose directory does not exist must return 404, not 500."""
    with patch("agentception.routes.control.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        mock_settings.repo_dir = tmp_path
        mock_settings.gh_repo = "cgcardona/maestro"

        response = client.post("/api/control/kill/does-not-exist")

    assert response.status_code == 404


# ── No .agent-task (no issue number) ─────────────────────────────────────────


def test_kill_worktree_without_agent_task_still_succeeds(
    client: TestClient,
    tmp_worktree_no_task: Path,
) -> None:
    """Kill must succeed even when .agent-task is absent (no issue number to clear)."""
    slug = tmp_worktree_no_task.name
    parent = tmp_worktree_no_task.parent

    async def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    with (
        patch("agentception.routes.control.settings") as mock_settings,
        patch("agentception.routes.control._run", side_effect=fake_run),
    ):
        mock_settings.worktrees_dir = parent
        mock_settings.repo_dir = Path("/repo")
        mock_settings.gh_repo = "cgcardona/maestro"

        response = client.post(f"/api/control/kill/{slug}")

    assert response.status_code == 200
    assert response.json() == {"killed": slug}
