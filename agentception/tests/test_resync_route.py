from __future__ import annotations

"""Tests for POST /api/control/resync-issues."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.services.resync_service import GitHubAPIError, ResyncResult


@pytest.fixture()
def client() -> TestClient:
    """Synchronous test client for the FastAPI app."""
    return TestClient(app)


def test_resync_endpoint_success(client: TestClient) -> None:
    """POST /api/control/resync-issues returns 200 with ok, open, closed, upserted."""
    mock_result = ResyncResult(open=10, closed=5, upserted=15)

    with patch(
        "agentception.routes.api.resync.resync_all_issues",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        response = client.post("/api/control/resync-issues")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["open"] == 10
    assert data["closed"] == 5
    assert data["upserted"] == 15


def test_resync_endpoint_github_failure(client: TestClient) -> None:
    """POST /api/control/resync-issues returns 503 with ok: false when GitHub API raises."""
    with patch(
        "agentception.routes.api.resync.resync_all_issues",
        new_callable=AsyncMock,
        side_effect=GitHubAPIError("GitHub API GET /repos/x/y/issues returned 503"),
    ):
        response = client.post("/api/control/resync-issues")

    assert response.status_code == 503
    data = response.json()
    assert data["ok"] is False
    assert "error" in data
    assert "503" in data["error"]


def test_resync_endpoint_custom_repo(client: TestClient) -> None:
    """POST /api/control/resync-issues?repo=owner/repo passes the repo to the service."""
    mock_result = ResyncResult(open=3, closed=2, upserted=5)

    with patch(
        "agentception.routes.api.resync.resync_all_issues",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_resync:
        response = client.post("/api/control/resync-issues?repo=owner/myrepo")

    assert response.status_code == 200
    mock_resync.assert_called_once_with("owner/myrepo")
