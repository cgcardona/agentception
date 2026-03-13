"""Tests for the AgentCeption pipeline control endpoints.

Covers every endpoint in ``agentception/routes/api/control.py``:

  POST /api/control/pause            → creates sentinel, toast header
  POST /api/control/resume           → removes sentinel, toast header
  GET  /api/control/status           → reflects sentinel state
  POST /api/control/trigger-poll     → fires async tick
  GET  /api/control/active-label     → returns label / pinned / pin
  PUT  /api/control/active-label     → pin a label; 400 for invalid label
  DELETE /api/control/active-label   → clear pin
  POST /api/control/sweep            → dry-run and live paths

All tests are synchronous and use temporary directories / mock patches so
they never touch the real repository filesystem or any live GitHub API.

Run targeted:
    pytest agentception/tests/test_agentception_controls.py -v
"""
from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import PipelineConfig


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Generator[TestClient, None, None]:
    """Test client with a temporary repo_dir so sentinel writes stay isolated.

    Module-scoped to avoid repeated TestClient lifespan startup/teardown which
    accumulates orphaned asyncio child-watcher threads (asyncio-waitpid-N) that
    eventually cause the event loop cleanup to hang.  Each test re-patches
    _SENTINEL to its own function-scoped tmp_path, so isolation is preserved.
    """
    sentinel = tmp_path_factory.mktemp("client") / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        with TestClient(app) as c:
            yield c


@pytest.fixture(scope="module")
def client_paused(tmp_path_factory: pytest.TempPathFactory) -> Generator[TestClient, None, None]:
    """Test client with the sentinel file pre-created (pipeline already paused).

    Module-scoped for the same reasons as ``client``.  Tests that need the
    sentinel to exist at their own tmp_path must call ``sentinel.touch()``
    themselves before asserting — they all re-patch _SENTINEL anyway.
    """
    sentinel = tmp_path_factory.mktemp("client_paused") / ".pipeline-pause"
    sentinel.touch()
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        with TestClient(app) as c:
            yield c


# ── POST /api/control/pause ───────────────────────────────────────────────────


def test_pause_creates_sentinel_file(tmp_path: Path, client: TestClient) -> None:
    """POST /api/control/pause must create the sentinel file on disk."""
    sentinel = tmp_path / ".pipeline-pause"
    assert not sentinel.exists(), "Sentinel must not exist before pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client.post("/api/control/pause")
    assert response.status_code == 200
    assert sentinel.exists(), "Sentinel must be created after pause"


def test_pause_returns_paused_true(tmp_path: Path, client: TestClient) -> None:
    """POST /api/control/pause must return {paused: true}."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client.post("/api/control/pause")
    assert response.status_code == 200
    assert response.json() == {"paused": True}


def test_pause_idempotent(tmp_path: Path, client_paused: TestClient) -> None:
    """POST /api/control/pause when already paused must succeed without error."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client_paused.post("/api/control/pause")
    assert response.status_code == 200
    assert response.json() == {"paused": True}


def test_pause_returns_hx_trigger_toast_header(tmp_path: Path, client: TestClient) -> None:
    """POST /api/control/pause must emit an HX-Trigger toast header for HTMX."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client.post("/api/control/pause")
    assert response.status_code == 200
    raw = response.headers.get("hx-trigger", "")
    payload = json.loads(raw)
    assert payload["toast"]["type"] == "warning"
    assert "paused" in payload["toast"]["message"].lower()


# ── POST /api/control/resume ──────────────────────────────────────────────────


def test_resume_deletes_sentinel_file(tmp_path: Path, client_paused: TestClient) -> None:
    """POST /api/control/resume must remove the sentinel file when it exists."""
    sentinel = tmp_path / ".pipeline-pause"
    sentinel.touch()  # each test owns its sentinel; re-patch below directs the route here
    assert sentinel.exists(), "Sentinel must exist before resume"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client_paused.post("/api/control/resume")
    assert response.status_code == 200
    assert not sentinel.exists(), "Sentinel must be gone after resume"


def test_resume_returns_paused_false(tmp_path: Path, client_paused: TestClient) -> None:
    """POST /api/control/resume must return {paused: false}."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client_paused.post("/api/control/resume")
    assert response.status_code == 200
    assert response.json() == {"paused": False}


def test_resume_idempotent_when_not_paused(tmp_path: Path, client: TestClient) -> None:
    """POST /api/control/resume when not paused must succeed without error."""
    sentinel = tmp_path / ".pipeline-pause"
    assert not sentinel.exists()
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client.post("/api/control/resume")
    assert response.status_code == 200
    assert response.json() == {"paused": False}


def test_resume_returns_hx_trigger_toast_header(tmp_path: Path, client_paused: TestClient) -> None:
    """POST /api/control/resume must emit an HX-Trigger toast header for HTMX."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client_paused.post("/api/control/resume")
    assert response.status_code == 200
    raw = response.headers.get("hx-trigger", "")
    payload = json.loads(raw)
    assert payload["toast"]["type"] == "success"
    assert "resumed" in payload["toast"]["message"].lower()


# ── GET /api/control/status ───────────────────────────────────────────────────


def test_status_reflects_sentinel_state_running(tmp_path: Path, client: TestClient) -> None:
    """GET /api/control/status must return {paused: false} when sentinel is absent."""
    sentinel = tmp_path / ".pipeline-pause"
    assert not sentinel.exists()
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client.get("/api/control/status")
    assert response.status_code == 200
    assert response.json() == {"paused": False}


def test_status_reflects_sentinel_state_paused(tmp_path: Path, client_paused: TestClient) -> None:
    """GET /api/control/status must return {paused: true} when sentinel is present."""
    sentinel = tmp_path / ".pipeline-pause"
    sentinel.touch()  # each test owns its sentinel; re-patch below directs the route here
    assert sentinel.exists()
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        response = client_paused.get("/api/control/status")
    assert response.status_code == 200
    assert response.json() == {"paused": True}


def test_status_updates_after_pause(tmp_path: Path, client: TestClient) -> None:
    """GET /api/control/status must reflect paused=true immediately after a pause call."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        client.post("/api/control/pause")
        response = client.get("/api/control/status")
    assert response.json() == {"paused": True}


def test_status_updates_after_resume(tmp_path: Path, client_paused: TestClient) -> None:
    """GET /api/control/status must reflect paused=false immediately after a resume call."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        client_paused.post("/api/control/resume")
        response = client_paused.get("/api/control/status")
    assert response.json() == {"paused": False}


# ── POST /api/control/trigger-poll ───────────────────────────────────────────


def test_trigger_poll_returns_triggered_true(client: TestClient) -> None:
    """POST /api/control/trigger-poll must return {triggered: true}.

    The actual poll tick runs asynchronously; we verify only the HTTP response
    shape and that the endpoint does not raise.
    """
    with patch("agentception.poller.tick", return_value=None):
        response = client.post("/api/control/trigger-poll")
    assert response.status_code == 200
    assert response.json() == {"triggered": True}


# ── GET /api/control/active-label ─────────────────────────────────────────────


def test_active_label_get_returns_auto_resolved_label(client: TestClient) -> None:
    """GET /api/control/active-label must return the auto-resolved label when no pin is set."""
    with (
        patch("agentception.routes.api.control.get_pin", return_value=None),
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="ac/5-plan",
        ),
    ):
        response = client.get("/api/control/active-label")
    assert response.status_code == 200
    data = response.json()
    assert data == {"label": "ac/5-plan", "pinned": False, "pin": None}


def test_active_label_get_returns_pinned_label(client: TestClient) -> None:
    """GET /api/control/active-label must report pinned=true and the pin value when a pin is active."""
    with (
        patch("agentception.routes.api.control.get_pin", return_value="ac/3-implement"),
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="ac/3-implement",
        ),
    ):
        response = client.get("/api/control/active-label")
    assert response.status_code == 200
    data = response.json()
    assert data == {"label": "ac/3-implement", "pinned": True, "pin": "ac/3-implement"}


# ── PUT /api/control/active-label ─────────────────────────────────────────────


def test_active_label_pin_valid_label_returns_200(client: TestClient) -> None:
    """PUT /api/control/active-label must set the pin and return the pinned status."""
    config = PipelineConfig(active_labels_order=["ac/5-plan", "ac/3-implement"])
    with (
        patch(
            "agentception.routes.api.control.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=config,
        ),
        patch("agentception.routes.api.control.set_pin") as mock_set,
    ):
        response = client.put("/api/control/active-label", json={"label": "ac/5-plan"})
    assert response.status_code == 200
    data = response.json()
    assert data["label"] == "ac/5-plan"
    assert data["pinned"] is True
    assert data["pin"] == "ac/5-plan"
    mock_set.assert_called_once_with("ac/5-plan")


def test_active_label_pin_invalid_label_returns_400(client: TestClient) -> None:
    """PUT /api/control/active-label must return 400 when the label is not in active_labels_order."""
    config = PipelineConfig(active_labels_order=["ac/5-plan"])
    with patch(
        "agentception.routes.api.control.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=config,
    ):
        response = client.put("/api/control/active-label", json={"label": "ac/9-unknown"})
    assert response.status_code == 400
    assert "ac/9-unknown" in response.json()["detail"]


# ── DELETE /api/control/active-label ──────────────────────────────────────────


def test_active_label_unpin_returns_auto_resolved(client: TestClient) -> None:
    """DELETE /api/control/active-label must clear the pin and return the auto-resolved label."""
    with (
        patch("agentception.routes.api.control.clear_pin") as mock_clear,
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="ac/5-plan",
        ),
    ):
        response = client.delete("/api/control/active-label")
    assert response.status_code == 200
    data = response.json()
    assert data == {"label": "ac/5-plan", "pinned": False, "pin": None}
    mock_clear.assert_called_once()


# ── POST /api/control/sweep ───────────────────────────────────────────────────


def _make_async_subprocess_noop() -> MagicMock:
    """Return a mock for asyncio.create_subprocess_exec that always succeeds."""
    async def _noop(*_args: str, **_kwargs: object) -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        return proc

    return MagicMock(side_effect=_noop)


def test_sweep_clean_state_returns_empty_results(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/control/sweep with no stale branches or claims must return all-empty lists."""
    with (
        patch(
            "agentception.readers.git.list_git_worktrees",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.readers.git.list_git_branches",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.readers.github.get_wip_issues",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.intelligence.guards.detect_stale_claims",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.routes.api.control.asyncio.create_subprocess_exec", _make_async_subprocess_noop()),
        patch("agentception.routes.api.control.settings") as mock_settings,
    ):
        mock_settings.repo_dir = tmp_path
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        response = client.post("/api/control/sweep")
    assert response.status_code == 200
    data = response.json()
    assert data["deleted_branches"] == []
    assert data["cleared_wip_labels"] == []
    assert data["errors"] == []


def test_sweep_dry_run_reports_stale_branch_without_deleting(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/control/sweep?dry_run=true must report stale branches but not run git branch -D."""
    exec_mock = _make_async_subprocess_noop()
    with (
        patch(
            "agentception.readers.git.list_git_worktrees",
            new_callable=AsyncMock,
            return_value=[],  # no live worktrees → every agent branch is stale
        ),
        patch(
            "agentception.readers.git.list_git_branches",
            new_callable=AsyncMock,
            return_value=[{"name": "agent/issue-42", "is_agent_branch": True}],
        ),
        patch(
            "agentception.readers.github.get_wip_issues",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.intelligence.guards.detect_stale_claims",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.routes.api.control.asyncio.create_subprocess_exec", exec_mock),
        patch("agentception.routes.api.control.settings") as mock_settings,
    ):
        mock_settings.repo_dir = tmp_path
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        response = client.post("/api/control/sweep?dry_run=true")
    assert response.status_code == 200
    data = response.json()
    assert "agent/issue-42" in data["deleted_branches"]
    # dry_run=True → subprocess must NOT have been called to delete the branch
    exec_mock.assert_not_called()


def test_sweep_live_branch_not_included_in_deleted(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/control/sweep must skip branches that have a live worktree checked out."""
    with (
        patch(
            "agentception.readers.git.list_git_worktrees",
            new_callable=AsyncMock,
            return_value=[{"branch": "agent/issue-7", "is_main": False}],
        ),
        patch(
            "agentception.readers.git.list_git_branches",
            new_callable=AsyncMock,
            return_value=[{"name": "agent/issue-7", "is_agent_branch": True}],
        ),
        patch(
            "agentception.readers.github.get_wip_issues",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.intelligence.guards.detect_stale_claims",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.routes.api.control.asyncio.create_subprocess_exec", _make_async_subprocess_noop()),
        patch("agentception.routes.api.control.settings") as mock_settings,
    ):
        mock_settings.repo_dir = tmp_path
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        response = client.post("/api/control/sweep")
    assert response.status_code == 200
    assert response.json()["deleted_branches"] == []


def test_sweep_clears_stale_wip_labels(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/control/sweep must call clear_wip_label for each stale claim."""

    class _FakeClaim:
        issue_number: int = 99

    with (
        patch(
            "agentception.readers.git.list_git_worktrees",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.readers.git.list_git_branches",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.readers.github.get_wip_issues",
            new_callable=AsyncMock,
            return_value=[{"number": 99}],
        ),
        patch(
            "agentception.intelligence.guards.detect_stale_claims",
            new_callable=AsyncMock,
            return_value=[_FakeClaim()],
        ),
        patch(
            "agentception.readers.github.clear_wip_label",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch("agentception.routes.api.control.asyncio.create_subprocess_exec", _make_async_subprocess_noop()),
        patch("agentception.routes.api.control.settings") as mock_settings,
    ):
        mock_settings.repo_dir = tmp_path
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        response = client.post("/api/control/sweep")
    assert response.status_code == 200
    data = response.json()
    assert 99 in data["cleared_wip_labels"]
    mock_clear.assert_called_once_with(99)


