from __future__ import annotations

"""Tests for the global toast notification system (issue #744).

Verifies:
- base.html renders the toast container and toastStore script.
- kill, pause, resume, and trigger-poll endpoints return HX-Trigger toast headers.

Run targeted:
    pytest agentception/tests/test_toast.py -v
"""

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


# ── base.html: toast infrastructure is present ────────────────────────────────


def test_base_template_has_toast_container(client: TestClient) -> None:
    """GET / must render base.html which includes the toast container div."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "toast-container" in resp.text
    assert "toastStore" in resp.text


# ── kill endpoint: HX-Trigger toast header ────────────────────────────────────


def test_kill_response_has_hx_trigger_toast(client: TestClient, tmp_path: Path) -> None:
    """POST /api/control/kill/{slug} on a live worktree returns HX-Trigger with toast key."""
    worktree = tmp_path / "issue-999"
    worktree.mkdir()

    async def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    with (
        patch("agentception.routes.control.settings") as mock_settings,
        patch("agentception.routes.control._run", side_effect=fake_run),
    ):
        mock_settings.worktrees_dir = tmp_path
        mock_settings.repo_dir = Path("/repo")
        mock_settings.gh_repo = "cgcardona/agentception"

        resp = client.post("/api/control/kill/issue-999")

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "toast" in trigger
    assert trigger["toast"]["type"] == "success"


# ── pause/resume/poll: HX-Trigger toast headers ───────────────────────────────


def test_pause_response_has_hx_trigger_toast(client: TestClient, tmp_path: Path) -> None:
    """POST /api/control/pause must return HX-Trigger header with warning toast."""
    sentinel = tmp_path / ".pipeline-pause"
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        resp = client.post("/api/control/pause")

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "toast" in trigger
    assert trigger["toast"]["type"] == "warning"


def test_resume_response_has_hx_trigger_toast(client: TestClient, tmp_path: Path) -> None:
    """POST /api/control/resume must return HX-Trigger header with success toast."""
    sentinel = tmp_path / ".pipeline-pause"
    sentinel.touch()
    with patch("agentception.routes.api.control._SENTINEL", sentinel):
        resp = client.post("/api/control/resume")

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "toast" in trigger
    assert trigger["toast"]["type"] == "success"


def test_trigger_poll_response_has_hx_trigger_toast(client: TestClient) -> None:
    """POST /api/control/trigger-poll must return HX-Trigger header with info toast."""
    with patch("agentception.poller.tick", new_callable=AsyncMock):
        resp = client.post("/api/control/trigger-poll")

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "toast" in trigger
    assert trigger["toast"]["type"] == "info"
