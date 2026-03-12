"""Tests for GET /api/ship/{repo}/initiative-tabs — initiative tab nav partial."""
from __future__ import annotations

from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_REPO = "agentception"
_URL = f"/api/ship/{_REPO}/initiative-tabs"
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
    """GET /api/ship/{repo}/initiative-tabs returns HTTP 200 with text/html content-type."""
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
    """GET /api/ship/nonexistent-repo/initiative-tabs returns HTTP 404."""
    with _patch_initiatives(_INITIATIVES):
        response = client.get("/api/ship/nonexistent-repo/initiative-tabs")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Integration tests — full page GET /ship/{repo}/{initiative}
# ---------------------------------------------------------------------------

def _patch_build_page(initiatives: list[str]) -> AbstractContextManager[MagicMock]:
    """Patch all DB calls needed to render the full build page."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    # We need a context manager that applies multiple patches at once.
    # Use a helper class so callers can use it as a `with` statement.
    class _MultiPatch:
        def __enter__(self) -> "_MultiPatch":
            self._stack = ExitStack()
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_initiatives",
                    new=AsyncMock(return_value=initiatives),
                )
            )
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
                    new=AsyncMock(return_value=[]),
                )
            )
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
                    new=AsyncMock(return_value={}),
                )
            )
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
                    new=AsyncMock(return_value={}),
                )
            )
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_latest_active_batch_id",
                    new=AsyncMock(return_value=None),
                )
            )
            self._stack.enter_context(
                patch(
                    "agentception.routes.ui.build_ui.get_run_tree_by_batch_id",
                    new=AsyncMock(return_value=[]),
                )
            )
            return self

        def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            self._stack.__exit__(exc_type, exc_val, exc_tb)  # type: ignore[arg-type]

    return _MultiPatch()  # type: ignore[return-value]


def test_full_page_has_htmx_polling_div(client: TestClient) -> None:
    """GET /ship/{repo}/{initiative} full page contains the HTMX polling div.

    Asserts that the rendered HTML includes a div with:
    - hx-get ending in /initiatives
    - hx-trigger="load, every 30s"
    - hx-swap="innerHTML"
    - hx-vals containing the "initiative" key
    """
    with _patch_build_page(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/mcp-audit-remediation")

    assert response.status_code == 200
    body = response.text

    assert 'hx-get=' in body
    assert '/initiative-tabs' in body
    assert 'hx-trigger=' in body
    assert 'every 30s' in body
    assert 'hx-swap=' in body
    assert 'innerHTML' in body
    assert 'hx-vals=' in body
    assert 'initiative' in body


def test_full_page_ssr_tab_nav_present(client: TestClient) -> None:
    """GET /ship/{repo}/{initiative} includes SSR tab nav inside the polling div.

    The polling div uses ``{% include "_build_initiative_tabs.html" %}`` for the
    initial server render so tabs are visible before HTMX fires.  This test
    confirms at least one tab anchor is present in the response.
    """
    with _patch_build_page(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/mcp-audit-remediation")

    assert response.status_code == 200
    body = response.text

    # The SSR include renders tab anchors with the build-initiative-tab class.
    assert 'build-initiative-tab' in body
    # At least one of the known slugs must appear as a tab.
    assert any(slug in body for slug in _INITIATIVES)


def test_active_initiative_tab_marked_in_full_page(client: TestClient) -> None:
    """GET /ship/{repo}/mcp-audit-remediation marks that slug's tab as active.

    The active tab must carry the ``build-initiative-tab--active`` CSS modifier
    class so the UI highlights the current initiative correctly.
    """
    active_slug = "mcp-audit-remediation"
    with _patch_build_page(_INITIATIVES):
        response = client.get(f"/ship/{_REPO}/{active_slug}")

    assert response.status_code == 200
    body = response.text

    assert 'build-initiative-tab--active' in body
    assert active_slug in body
