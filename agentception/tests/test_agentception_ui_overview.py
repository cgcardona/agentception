"""Tests for the AgentCeption pipeline overview UI (AC-006).

Covers the ``GET /`` overview page and the ``GET /api/pipeline`` JSON endpoint.
All tests are synchronous — no live GitHub calls, no background polling.

Run targeted:
    pytest agentception/tests/test_agentception_ui_overview.py -v
"""
from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import AgentNode, AgentStatus, BoardIssue, PipelineState


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan (poller is started but immediately cancelled)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def empty_state() -> PipelineState:
    """PipelineState with zero agents — simulates pre-first-tick."""
    return PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )


@pytest.fixture()
def populated_state() -> PipelineState:
    """PipelineState with one implementing agent and one alert."""
    return PipelineState(
        active_label="agentception/0-scaffold",
        issues_open=3,
        prs_open=2,
        agents=[
            AgentNode(
                id="issue-615",
                role="python-developer",
                status=AgentStatus.IMPLEMENTING,
                issue_number=615,
                branch="feat/issue-615-overview-ui",
                batch_id="eng-20260302T001107Z-2810",
            )
        ],
        alerts=["Stale claim on #600"],
        polled_at=time.time(),
    )


# ── GET / — overview page ─────────────────────────────────────────────────────


def test_overview_returns_200(client: TestClient, empty_state: PipelineState) -> None:
    """GET / must return HTTP 200 even when no agents are active."""
    with patch("agentception.routes.ui.overview.get_state", return_value=empty_state):
        response = client.get("/")
    assert response.status_code == 200


def test_overview_contains_tree_element(client: TestClient, empty_state: PipelineState) -> None:
    """GET / response HTML must contain a ``#tree`` element for Alpine.js to target."""
    with patch("agentception.routes.ui.overview.get_state", return_value=empty_state):
        response = client.get("/")
    assert 'id="tree"' in response.text


def test_overview_sse_connect_attribute(client: TestClient, empty_state: PipelineState) -> None:
    """GET / HTML must load app.js, which wires the EventSource to /events."""
    with patch("agentception.routes.ui.overview.get_state", return_value=empty_state):
        response = client.get("/")
    # EventSource logic lives in app.js; verify the script is loaded and
    # the pipelineDashboard x-data binding is present in the HTML.
    assert "/static/app.js" in response.text
    assert "pipelineDashboard(" in response.text


def test_overview_contains_summary_bar(client: TestClient, empty_state: PipelineState) -> None:
    """GET / HTML must include the pipeline summary bar."""
    with patch("agentception.routes.ui.overview.get_state", return_value=empty_state):
        response = client.get("/")
    assert "pipeline-summary-bar" in response.text


def test_overview_renders_when_no_state(client: TestClient) -> None:
    """GET / must render without error when the poller hasn't ticked yet (get_state returns None)."""
    with patch("agentception.routes.ui.overview.get_state", return_value=None):
        response = client.get("/")
    assert response.status_code == 200
    assert "Agentception" in response.text


def test_overview_alert_banner_present(
    client: TestClient, populated_state: PipelineState
) -> None:
    """GET / HTML must include the alert-banner CSS class when alerts are non-empty."""
    with patch("agentception.routes.ui.overview.get_state", return_value=populated_state):
        response = client.get("/")
    assert "alert-banner" in response.text


def test_overview_status_badge_classes_in_html(
    client: TestClient, populated_state: PipelineState
) -> None:
    """GET / HTML must include status-badge CSS classes for the Alpine.js template."""
    with patch("agentception.routes.ui.overview.get_state", return_value=populated_state):
        response = client.get("/")
    assert "status-badge" in response.text


# ── GET /api/pipeline — JSON endpoint ────────────────────────────────────────


def test_pipeline_api_returns_json(client: TestClient, empty_state: PipelineState) -> None:
    """GET /api/pipeline must return a valid PipelineState JSON payload."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=empty_state):
        response = client.get("/api/pipeline")
    assert response.status_code == 200
    data = response.json()
    assert "agents" in data
    assert "issues_open" in data
    assert "prs_open" in data
    assert "polled_at" in data


def test_pipeline_api_returns_json_when_no_state(client: TestClient) -> None:
    """GET /api/pipeline must return an empty-but-valid PipelineState when poller hasn't ticked."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=None):
        response = client.get("/api/pipeline")
    assert response.status_code == 200
    data = response.json()
    assert data["agents"] == []
    assert data["issues_open"] == 0
    assert data["prs_open"] == 0


def test_pipeline_api_reflects_populated_state(
    client: TestClient, populated_state: PipelineState
) -> None:
    """GET /api/pipeline must expose agent and alert data from the current PipelineState."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=populated_state):
        response = client.get("/api/pipeline")
    assert response.status_code == 200
    data = response.json()
    assert data["active_label"] == "agentception/0-scaffold"
    assert data["issues_open"] == 3
    assert len(data["agents"]) == 1
    assert data["agents"][0]["role"] == "python-developer"
    assert data["agents"][0]["status"] == "implementing"
    assert len(data["alerts"]) == 1


# ── Batch grouping ────────────────────────────────────────────────────────────


@pytest.fixture()
def two_batch_state() -> PipelineState:
    """PipelineState with board_issues spanning two distinct phase_labels (batches)."""
    return PipelineState(
        active_label="ac-ui",
        issues_open=3,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
        board_issues=[
            BoardIssue(number=101, title="Alpha Issue 1", phase_label="ac-ui/1-alpha"),
            BoardIssue(number=102, title="Alpha Issue 2", phase_label="ac-ui/1-alpha"),
            BoardIssue(number=201, title="Beta Issue 1", phase_label="ac-ui/2-beta"),
        ],
    )


def test_overview_groups_by_batch(
    client: TestClient, two_batch_state: PipelineState
) -> None:
    """GET / HTML must render one batch-group section per distinct phase_label."""
    with patch("agentception.routes.ui.overview.get_state", return_value=two_batch_state):
        response = client.get("/")
    assert response.status_code == 200
    assert response.text.count('class="batch-group') == 2


# ── Alert deduplication ───────────────────────────────────────────────────────


def test_overview_alert_rendered_once(
    client: TestClient, empty_state: PipelineState
) -> None:
    """GET / must render the 'Pipeline paused' banner exactly once when paused.

    The banner contains the text in a single ``<span>`` — not once per issue
    card or once per alert entry.  Checks the span tag to avoid counting
    aria-label attribute occurrences that share the same string.
    """
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=empty_state),
        patch("pathlib.Path.exists", return_value=True),
    ):
        response = client.get("/")
    assert response.status_code == 200
    assert response.text.count("<span>Pipeline paused</span>") == 1
