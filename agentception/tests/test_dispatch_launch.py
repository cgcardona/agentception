"""Tests for POST /api/dispatch/launch endpoint.

Covers:
  - Returns ok=True and file contents when agent-conductor.md exists.
  - Returns ok=False and error message when agent-conductor.md is absent.

Run targeted:
    pytest agentception/tests/test_dispatch_launch.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_launch_dispatcher_prompt_returns_prompt_when_file_exists(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/dispatch/launch returns ok=True and the file contents when agent-conductor.md exists."""
    conductor_dir = tmp_path / ".agentception"
    conductor_dir.mkdir()
    conductor_file = conductor_dir / "agent-conductor.md"
    conductor_file.write_text("# Conductor prompt", encoding="utf-8")

    with patch("agentception.routes.api.dispatch.settings") as mock_settings:
        mock_settings.repo_dir = str(tmp_path)
        res = client.post("/api/dispatch/launch")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["prompt"] == "# Conductor prompt"
    assert body["error"] is None


def test_launch_dispatcher_prompt_returns_error_when_file_missing(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /api/dispatch/launch returns ok=False and an error message when agent-conductor.md is absent."""
    with patch("agentception.routes.api.dispatch.settings") as mock_settings:
        mock_settings.repo_dir = str(tmp_path)
        res = client.post("/api/dispatch/launch")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["prompt"] == ""
    assert "agent-conductor.md not found" in body["error"]
