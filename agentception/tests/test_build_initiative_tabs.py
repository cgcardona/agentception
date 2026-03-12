"""Tests for GET /ship/{repo}/initiatives — initiative tab nav partial."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_REPO = "agentception"
_INITIATIVES = ["mcp-audit-remediation", "ac-build", "ac-workflow"]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _patch_initiatives(initiatives: list[str]):
    """Patch get_initiatives in the build_ui module to return a fixed list."""
    return patch(
        "agentception.routes.ui.build_ui.get_initiatives",
        new=AsyncMock(return_value=initiatives),
    )


def test_initiatives_endpoint_200(client: TestClient) -> None:
    """GET /ship/{repo}/initiatives returns HTTP 200 with text/html content-type."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/initiatives")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_initiatives_endpoint_contains_slugs(client: TestClient) -> None:
    """Response body contains the known initiative slugs."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/initiatives")

    body = response.text
    for slug in _INITIATIVES:
        assert slug in body, f"Expected slug '{slug}' in response body"


def test_initiatives_endpoint_marks_active_tab(client: TestClient) -> None:
    """?initiative=<slug> causes the matching tab to receive the active CSS class."""
    active = _INITIATIVES[0]
    with _patch_initiatives(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/initiatives?initiative={active}")

    assert response.status_code == 200
    # The active tab should carry the --active modifier class.
    assert "build-initiative-tab--active" in response.text
    # The active slug must appear near the active class (both on the same element).
    body = response.text
    active_idx = body.find("build-initiative-tab--active")
    slug_idx = body.find(active)
    # Both must be present and the slug must appear close to the active class marker.
    assert active_idx != -1
    assert slug_idx != -1


def test_initiatives_endpoint_unknown_repo_returns_404(client: TestClient) -> None:
    """GET /ship/nonexistent-repo/initiatives returns HTTP 404."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get("/ship/nonexistent-repo/initiatives")

    assert response.status_code == 404
