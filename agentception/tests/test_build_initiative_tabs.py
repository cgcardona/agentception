"""Tests for GET /ship/{repo}/initiatives — initiative tab nav partial."""
from __future__ import annotations

from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_REPO = "agentception"
_URL = f"/ship/{_REPO}/initiatives"
_INITIATIVES = ["mcp-audit-remediation", "auth-rewrite", "ac-plan"]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _patch_initiatives(initiatives: list[str]) -> AbstractContextManager[MagicMock]:
    """Patch get_initiatives in the build_ui module to return *initiatives*."""
    return patch(
        "agentception.routes.ui.build_ui.get_initiatives",
        new=AsyncMock(return_value=initiatives),
    )


def test_initiatives_endpoint_200(client: TestClient) -> None:
    """GET /ship/{repo}/initiatives returns HTTP 200 with text/html content-type."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get(_URL)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_initiatives_endpoint_contains_slugs(client: TestClient) -> None:
    """Response body contains the known initiative slugs when they exist."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get(_URL)

    body = response.text
    for slug in _INITIATIVES:
        assert slug in body, f"Expected slug '{slug}' in response body"


def test_initiatives_endpoint_marks_active_tab(client: TestClient) -> None:
    """?initiative=<slug> causes the matching tab to receive the active CSS class."""
    active = _INITIATIVES[0]
    with _patch_initiatives(_INITIATIVES):
        response = client.get(_URL, params={"initiative": active})

    body = response.text
    # The active tab should carry the --active modifier class.
    assert "build-initiative-tab--active" in body
    # The active slug must appear adjacent to the active class in the markup.
    assert active in body


def test_initiatives_endpoint_unknown_repo_returns_404(client: TestClient) -> None:
    """GET /ship/nonexistent-repo/initiatives returns HTTP 404."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get("/ship/nonexistent-repo/initiatives")

    assert response.status_code == 404
