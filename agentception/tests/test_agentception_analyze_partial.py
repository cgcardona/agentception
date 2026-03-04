from __future__ import annotations

"""Tests for the analyze partial endpoint (AC-406).

Covers ``POST /api/analyze/issue/{number}/partial`` which returns an HTMX
HTML fragment with analysis results for a single GitHub issue.

Run targeted:
    pytest agentception/tests/test_agentception_analyze_partial.py -v
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.analyzer import IssueAnalysis


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


def _make_analysis(
    number: int = 101,
    dependencies: list[int] | None = None,
    parallelism: str = "safe",
    conflict_risk: str = "none",
    modifies_files: list[str] | None = None,
    recommended_role: str = "python-developer",
    recommended_merge_after: int | None = None,
) -> IssueAnalysis:
    """Build a minimal IssueAnalysis for testing."""
    return IssueAnalysis(
        number=number,
        dependencies=dependencies or [],
        parallelism=parallelism,
        conflict_risk=conflict_risk,
        modifies_files=modifies_files or [],
        recommended_role=recommended_role,
        recommended_merge_after=recommended_merge_after,
    )


# ---------------------------------------------------------------------------
# test_analysis_partial_returns_html
# ---------------------------------------------------------------------------


def test_analysis_partial_returns_html(client: TestClient) -> None:
    """POST /api/analyze/issue/{number}/partial must return HTML with 200 OK."""
    analysis = _make_analysis(number=101)
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/101/partial")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "analysis-card" in response.text


def test_analysis_partial_returns_html_structure(client: TestClient) -> None:
    """Partial must include the analysis-card wrapper and labelled rows."""
    analysis = _make_analysis(number=202)
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/202/partial")
    assert response.status_code == 200
    # Key structural elements
    assert "analysis-card" in response.text
    assert "analysis-row" in response.text
    assert "analysis-label" in response.text


# ---------------------------------------------------------------------------
# test_analysis_partial_shows_deps
# ---------------------------------------------------------------------------


def test_analysis_partial_shows_deps(client: TestClient) -> None:
    """Partial must render dependency chips linking to each dep issue."""
    analysis = _make_analysis(number=303, dependencies=[614, 615], recommended_merge_after=615)
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/303/partial")
    assert response.status_code == 200
    # Both dep chips must appear
    assert "#614" in response.text
    assert "#615" in response.text
    # Chips link to GitHub issues
    assert "issues/614" in response.text
    assert "issues/615" in response.text


def test_analysis_partial_shows_no_deps_text(client: TestClient) -> None:
    """Partial must render 'None' when the issue has no dependencies."""
    analysis = _make_analysis(number=404, dependencies=[])
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/404/partial")
    assert response.status_code == 200
    assert "None" in response.text


def test_analysis_partial_shows_merge_after(client: TestClient) -> None:
    """Partial must render the MERGE_AFTER chip when recommended_merge_after is set."""
    analysis = _make_analysis(number=505, dependencies=[620], recommended_merge_after=620)
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/505/partial")
    assert response.status_code == 200
    assert "Merge after" in response.text
    assert "#620" in response.text


# ---------------------------------------------------------------------------
# test_analysis_partial_shows_role
# ---------------------------------------------------------------------------


def test_analysis_partial_shows_role(client: TestClient) -> None:
    """Partial must render the recommended role as a role chip."""
    analysis = _make_analysis(number=606, recommended_role="python-developer")
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/606/partial")
    assert response.status_code == 200
    assert "role-chip" in response.text
    assert "python-developer" in response.text


def test_analysis_partial_shows_database_architect_role(client: TestClient) -> None:
    """Partial must render database-architect role when analysis recommends it."""
    analysis = _make_analysis(number=707, recommended_role="database-architect")
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/707/partial")
    assert response.status_code == 200
    assert "database-architect" in response.text


# ---------------------------------------------------------------------------
# Parallelism and conflict risk badges
# ---------------------------------------------------------------------------


def test_analysis_partial_shows_parallelism_safe_badge(client: TestClient) -> None:
    """Partial must render a green 'Parallel safe' badge for safe issues."""
    analysis = _make_analysis(number=808, parallelism="safe")
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/808/partial")
    assert response.status_code == 200
    assert "Parallel safe" in response.text
    assert "badge--green" in response.text


def test_analysis_partial_shows_parallelism_risky_badge(client: TestClient) -> None:
    """Partial must render a yellow 'Risky' badge for risky issues."""
    analysis = _make_analysis(number=909, parallelism="risky")
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/909/partial")
    assert response.status_code == 200
    assert "Risky" in response.text
    assert "badge--yellow" in response.text


def test_analysis_partial_shows_serial_badge(client: TestClient) -> None:
    """Partial must render a red 'Serial only' badge for serial issues."""
    analysis = _make_analysis(number=111, parallelism="serial")
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(return_value=analysis),
    ):
        response = client.post("/api/analyze/issue/111/partial")
    assert response.status_code == 200
    assert "Serial only" in response.text
    assert "badge--red" in response.text


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_analysis_partial_404_on_unknown_issue(client: TestClient) -> None:
    """POST must return HTTP 404 when the GitHub CLI reports the issue not found."""
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(side_effect=RuntimeError("issue not found: #99999")),
    ):
        response = client.post("/api/analyze/issue/99999/partial")
    assert response.status_code == 404


def test_analysis_partial_500_on_github_error(client: TestClient) -> None:
    """POST must return HTTP 500 when the GitHub CLI exits with a non-zero status."""
    with patch(
        "agentception.routes.ui.overview.analyze_issue",
        new=AsyncMock(side_effect=RuntimeError("gh: authentication failed")),
    ):
        response = client.post("/api/analyze/issue/123/partial")
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Overview page integration — board_issues rendered
# ---------------------------------------------------------------------------


def test_overview_shows_analyze_button_for_unclaimed_issues(client: TestClient) -> None:
    """GET / must include the Analyze button markup and embed board_issues in initial state.

    The board sidebar is now fully Alpine-reactive: issue cards are rendered
    by `x-for` in the browser using `state.board_issues` from the SSE stream.
    We verify the server embeds the issue data in the serialised initial state
    JSON and includes the 'Analyze' button markup template in the HTML.
    """
    from agentception.models import BoardIssue, PipelineState

    state = PipelineState(
        active_label="ac-ui/0-critical-bugs",
        issues_open=1,
        prs_open=0,
        agents=[],
        board_issues=[BoardIssue(number=42, title="Add feature X")],
        polled_at=0.0,
    )
    with patch("agentception.routes.ui.overview.get_state", return_value=state):
        response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Issue data is embedded in the Alpine initial state JSON.
    assert "42" in html
    assert "Add feature X" in html
    # Analyze button template must be present in the page source.
    assert "Analyze" in html


def test_overview_hides_analyze_button_for_claimed_issues(client: TestClient) -> None:
    """GET / must NOT embed claimed issue data in board_issues initial state.

    The poller's _build_board_issues() filters out claimed issues before
    adding them to PipelineState.board_issues — so when state.board_issues
    is empty, issue #99 should not appear anywhere in the page source.
    """
    from agentception.models import PipelineState

    # board_issues is empty — claimed issues are filtered before reaching state.
    state = PipelineState.empty()
    with patch("agentception.routes.ui.overview.get_state", return_value=state):
        response = client.get("/")

    assert response.status_code == 200
    # Issue 99 must not appear in board_issues — it was claimed and filtered out.
    # We check for the JSON field "number": 99 rather than bare "99" which can
    # appear inside timestamps or other numeric values in the page.
    assert '"number": 99' not in response.text
    assert "issue-99" not in response.text
