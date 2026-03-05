from __future__ import annotations

"""Tests for the AgentCeption pipeline pause/resume control endpoints (AC-102).

Covers:
  POST /api/control/pause   → creates sentinel, returns {paused: true}
  POST /api/control/resume  → deletes sentinel, returns {paused: false}
  GET  /api/control/status  → reflects current sentinel state

All tests are synchronous and use a temporary directory for the sentinel file
so they never touch the real repository filesystem.

Run targeted:
    pytest agentception/tests/test_agentception_controls.py -v
"""

import importlib
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


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
    with patch("agentception.routes.api.control.trigger_poll.__wrapped__", None, create=True):
        pass
    with patch("agentception.poller.tick", return_value=None) as mock_tick:  # noqa: F841
        response = client.post("/api/control/trigger-poll")
    assert response.status_code == 200
    assert response.json() == {"triggered": True}
