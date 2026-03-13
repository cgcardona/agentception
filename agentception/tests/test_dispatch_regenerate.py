"""Tests for POST /api/dispatch/regenerate.

Covers:
  - Returns ok=True and a list of .md files when generate.py exits 0.
  - Returns ok=False and an error message when generate.py exits non-zero.

Run targeted:
    pytest agentception/tests/test_dispatch_regenerate.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def test_regenerate_prompts_success(client: TestClient) -> None:
    """POST /api/dispatch/regenerate returns ok=True and a list of .md files
    when generate.py exits with returncode 0."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    mock_rglob_files = [
        MagicMock(__str__=lambda self: "/repo/.agentception/agent-conductor.md"),
    ]

    with (
        patch(
            "agentception.routes.api.dispatch.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
        patch(
            "agentception.routes.api.dispatch.Path.rglob",
            return_value=iter(mock_rglob_files),
        ),
    ):
        resp = client.post("/api/dispatch/regenerate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None


def test_regenerate_prompts_failure(client: TestClient) -> None:
    """POST /api/dispatch/regenerate returns ok=False and an error message
    when generate.py exits with a non-zero returncode."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Something went wrong"))

    with patch(
        "agentception.routes.api.dispatch.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_proc),
    ):
        resp = client.post("/api/dispatch/regenerate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Something went wrong" in body["error"]
