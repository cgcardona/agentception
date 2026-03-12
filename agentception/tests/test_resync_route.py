from __future__ import annotations

"""Tests for POST /api/control/resync-issues."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_resync_issues_returns_200_on_success() -> None:
    """POST /api/control/resync-issues returns 200 with ok/open/closed/upserted on success."""
    from agentception.app import app

    with (
        patch(
            "agentception.routes.api.resync.settings",
        ) as mock_settings,
        patch(
            "agentception.routes.api.resync.resync_all_issues",
            new_callable=AsyncMock,
            return_value={"open": 5, "closed": 3, "upserted": 8},
        ),
    ):
        mock_settings.gh_repo = "owner/repo"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/control/resync-issues")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["open"] == 5
    assert body["closed"] == 3
    assert body["upserted"] == 8


@pytest.mark.anyio
async def test_resync_issues_returns_503_on_github_error() -> None:
    """POST /api/control/resync-issues returns 503 with ok:false and error when GitHub raises."""
    from agentception.app import app

    with (
        patch(
            "agentception.routes.api.resync.settings",
        ) as mock_settings,
        patch(
            "agentception.routes.api.resync.resync_all_issues",
            new_callable=AsyncMock,
            side_effect=RuntimeError("GitHub API unavailable"),
        ),
    ):
        mock_settings.gh_repo = "owner/repo"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/control/resync-issues")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert "GitHub API unavailable" in body["error"]


@pytest.mark.anyio
async def test_resync_issues_returns_422_when_no_repo_configured() -> None:
    """POST /api/control/resync-issues returns 422 with a clear message when GH_REPO is not set."""
    from agentception.app import app

    with patch(
        "agentception.routes.api.resync.settings",
    ) as mock_settings:
        mock_settings.gh_repo = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/control/resync-issues")

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert "GH_REPO" in body["error"]
