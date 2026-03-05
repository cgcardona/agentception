from __future__ import annotations

"""Tests for the AgentCeption pipeline config UI page (AC-305).

Covers:
- GET /config returns HTTP 200 and renders the config template.
- GET /config pre-populates current values from the config reader.
- Saving via PUT /api/config validates and persists the new values.

All tests use the synchronous TestClient — no live filesystem reads,
no background polling. The config reader is patched via AsyncMock so
tests remain independent of the on-disk pipeline-config.json.

Run targeted:
    pytest agentception/tests/test_agentception_ui_config.py -v
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import PipelineConfig
from agentception.readers.pipeline_config import _DEFAULTS


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


_DEFAULT_CONFIG = PipelineConfig.model_validate(_DEFAULTS)

_CUSTOM_CONFIG = PipelineConfig(
    coordinator_limits={"engineering-coordinator": 3, "qa-coordinator": 2},
    pool_size=6,
    active_labels_order=[
        "agentception/0-scaffold",
        "agentception/1-controls",
        "agentception/2-telemetry",
    ],
)


# ---------------------------------------------------------------------------
# test_config_page_returns_200
# ---------------------------------------------------------------------------


def test_config_page_returns_200(client: TestClient) -> None:
    """GET /config returns HTTP 200 and renders the config HTML page."""
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_DEFAULT_CONFIG,
    ):
        response = client.get("/config")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_config_page_returns_200_without_config_reader(client: TestClient) -> None:
    """GET /config still returns 200 even when the config reader raises an exception."""
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        side_effect=OSError("disk read error"),
    ):
        response = client.get("/config")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# test_config_page_shows_current_values
# ---------------------------------------------------------------------------


def test_config_page_shows_current_values(client: TestClient) -> None:
    """GET /config renders a page that contains the Alpine.js configPanel component."""
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_CUSTOM_CONFIG,
    ):
        response = client.get("/config")

    body = response.text
    # The page must invoke configPanel via x-data (function defined in app.js).
    assert "configPanel(" in body
    # Pool size slider must be present.
    assert 'id="slider-pool-size"' in body
    # Dynamic coordinator sliders are rendered via Alpine x-for — verify the template element.
    assert "coordinator_limits" in body
    # Label editor must be present.
    assert "label-list" in body
    # Save button must be present.
    assert "btn-save-config" in body
    # SSR hydration — custom config values must appear in the initial JS state
    # so the page reflects current settings before Alpine.js fetches the API.
    assert '"coordinator_limits"' in body
    assert '"pool_size"' in body


def test_config_page_contains_api_put_endpoint(client: TestClient) -> None:
    """GET /config page loads app.js which calls PUT /api/config on save."""
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_DEFAULT_CONFIG,
    ):
        response = client.get("/config")

    # The PUT /api/config call lives in app.js; verify the script is loaded
    # and the x-data binding that connects to it is present in the HTML.
    assert "/static/app.js" in response.text
    assert "configPanel(" in response.text


def test_config_page_nav_link_active(client: TestClient) -> None:
    """GET /config renders with the primary nav (Plan/Build/Ship) from the base template.

    The Config nav link was moved to secondary nav (currently hidden) in favour of the
    1-2-3 Plan → Build → Ship primary flow. The test verifies the primary nav is
    rendered and the page loads successfully.
    """
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_DEFAULT_CONFIG,
    ):
        response = client.get("/config")

    body = response.text
    assert response.status_code == 200
    # Primary nav links (Plan → Build → Ship) must be present in the base template.
    assert 'href="/plan"' in body
    assert 'href="/build"' in body
    assert 'href="/controls"' in body


# ---------------------------------------------------------------------------
# test_config_save_calls_put_api  (API-level, not browser-level)
# ---------------------------------------------------------------------------


def test_config_save_calls_put_api_valid_payload(client: TestClient) -> None:
    """PUT /api/config validates a correct payload and returns the saved config."""
    payload = {
        "coordinator_limits": {"engineering-coordinator": 2, "qa-coordinator": 1},
        "pool_size": 5,
        "active_labels_order": ["agentception/0-scaffold", "agentception/1-controls"],
    }
    saved = PipelineConfig.model_validate(payload)
    with patch(
        "agentception.routes.api.config.write_pipeline_config",
        new_callable=AsyncMock,
        return_value=saved,
    ):
        response = client.put("/api/config", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["coordinator_limits"] == {"engineering-coordinator": 2, "qa-coordinator": 1}
    assert body["pool_size"] == 5
    assert body["active_labels_order"] == ["agentception/0-scaffold", "agentception/1-controls"]


def test_config_save_calls_put_api_rejects_bad_payload(client: TestClient) -> None:
    """PUT /api/config returns 422 when required fields are missing."""
    response = client.put("/api/config", json={"coordinator_limits": {"engineering-coordinator": 2}})
    assert response.status_code == 422


def test_config_save_calls_put_api_slider_ranges(client: TestClient) -> None:
    """PUT /api/config accepts integer values matching slider ranges (1-4 for VPs, 1-8 pool)."""
    payload = {
        "coordinator_limits": {"engineering-coordinator": 4, "qa-coordinator": 4},
        "pool_size": 8,
        "active_labels_order": [],
    }
    saved = PipelineConfig.model_validate(payload)
    with patch(
        "agentception.routes.api.config.write_pipeline_config",
        new_callable=AsyncMock,
        return_value=saved,
    ):
        response = client.put("/api/config", json=payload)

    assert response.status_code == 200
    assert response.json()["coordinator_limits"] == {"engineering-coordinator": 4, "qa-coordinator": 4}
    assert response.json()["pool_size"] == 8


# ---------------------------------------------------------------------------
# AC-601: Project switcher in nav bar
# ---------------------------------------------------------------------------


def test_nav_shows_project_dropdown(client: TestClient) -> None:
    """Every HTML page contains the project-switcher nav element.

    The project switcher is rendered in the base template and is controlled
    by an Alpine.js component.  It is hidden via ``x-show`` when there are
    no configured projects, but the DOM element and the ``projectSwitcher``
    function must always be present so Alpine can initialise correctly.
    """
    with patch(
        "agentception.routes.ui.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_DEFAULT_CONFIG,
    ):
        response = client.get("/config")

    body = response.text
    # The DOM element with the id used by tests and the Alpine.js component.
    assert 'id="project-switcher"' in body
    # The Alpine.js data binding for the switcher.
    assert "projectSwitcher()" in body
    # The <select> that lists available projects.
    assert 'id="project-select"' in body
