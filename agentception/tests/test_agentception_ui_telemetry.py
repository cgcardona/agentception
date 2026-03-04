from __future__ import annotations

"""Tests for the AgentCeption telemetry UI page (AC-203, #743).

Covers:
- GET /telemetry returns HTTP 200 for both empty and non-empty wave history
- The page renders a wave table with sort controls when waves are present
- The page renders gracefully with an empty wave list
- Date filter inputs and CSV export button are present when waves exist

Run targeted:
    pytest agentception/tests/test_agentception_ui_telemetry.py -v
"""

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import AgentNode, AgentStatus
from agentception.telemetry import WaveSummary


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan (poller starts and is immediately cancelled)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def empty_waves() -> list[WaveSummary]:
    """Empty wave list — simulates a fresh deployment with no history."""
    return []


@pytest.fixture()
def populated_waves() -> list[WaveSummary]:
    """Two WaveSummary objects covering completed and active waves."""
    now = time.time()
    agent = AgentNode(
        id="issue-615",
        role="python-developer",
        status=AgentStatus.DONE,
        issue_number=615,
        batch_id="eng-batch-A",
    )
    return [
        WaveSummary(
            batch_id="eng-batch-A",
            started_at=now - 3600,
            ended_at=now - 3000,
            issues_worked=[615, 616],
            prs_opened=2,
            prs_merged=2,
            estimated_tokens=160000,
            estimated_cost_usd=1.632,
            agents=[agent],
        ),
        WaveSummary(
            batch_id="eng-batch-B",
            started_at=now - 900,
            ended_at=None,  # still active
            issues_worked=[620],
            prs_opened=0,
            prs_merged=0,
            estimated_tokens=0,
            estimated_cost_usd=0.0,
            agents=[],
        ),
    ]


# ── test_telemetry_page_returns_200 ──────────────────────────────────────────


def test_telemetry_page_returns_200(
    client: TestClient, populated_waves: list[WaveSummary]
) -> None:
    """GET /telemetry must return HTTP 200 when wave history is present."""
    with patch(
        "agentception.routes.ui.telemetry.aggregate_waves",
        new_callable=AsyncMock,
        return_value=populated_waves,
    ):
        response = client.get("/telemetry")
    assert response.status_code == 200


# ── test_telemetry_page_shows_wave_table ─────────────────────────────────────


def test_telemetry_page_shows_wave_table(
    client: TestClient, populated_waves: list[WaveSummary]
) -> None:
    """GET /telemetry must render the wave table with sort controls and export button."""
    with patch(
        "agentception.routes.ui.telemetry.aggregate_waves",
        new_callable=AsyncMock,
        return_value=populated_waves,
    ):
        response = client.get("/telemetry")

    assert response.status_code == 200
    html = response.text
    # Table must be present
    assert "telemetry-table" in html
    # Both batch IDs must appear in the embedded JSON blob
    assert "eng-batch-A" in html
    assert "eng-batch-B" in html
    # Sortable column headers must be present
    assert "telemetry-th--sortable" in html
    # Date range filter inputs must be present
    assert "telemetry-date-input" in html
    # CSV export button must be present
    assert "Export CSV" in html
    # Controls container must be present
    assert "telemetry-controls" in html
    # Summary stats bar must be present
    assert "pipeline-summary-bar" in html


# ── test_telemetry_page_empty_waves ──────────────────────────────────────────


def test_telemetry_page_empty_waves(
    client: TestClient, empty_waves: list[WaveSummary]
) -> None:
    """GET /telemetry must return HTTP 200 and an empty-state message when there are no waves."""
    with patch(
        "agentception.routes.ui.telemetry.aggregate_waves",
        new_callable=AsyncMock,
        return_value=empty_waves,
    ):
        response = client.get("/telemetry")

    assert response.status_code == 200
    html = response.text
    # Empty-state text must be shown
    assert "No wave history" in html
    # The table must NOT be rendered for an empty list
    assert "telemetry-table" not in html
    # Summary stats bar is still shown even with no waves
    assert "pipeline-summary-bar" in html
