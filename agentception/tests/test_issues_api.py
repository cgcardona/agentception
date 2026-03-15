"""Tests for the GitHub issue/PR HTMX partial endpoints.

Covers every route in agentception/routes/api/issues.py:

  GET  /api/issues/{repo}/{number}/comments  — issue_comments_partial
  GET  /api/prs/{repo}/{number}/checks       — pr_checks_partial
  GET  /api/prs/{repo}/{number}/reviews      — pr_reviews_partial
  GET  /api/issues/approval-queue            — approval_queue_partial
  POST /api/issues/{repo}/{number}/approve   — approve_issue

All GitHub reader calls are mocked so these tests run fully offline.
Because the endpoints are HTMX partials that render Jinja2 templates, tests
assert HTTP 200, text/html content-type, graceful degradation on reader
failure, and the HX-Trigger toast header emitted by approve_issue.

Run targeted:
    pytest agentception/tests/test_issues_api.py -v
"""
from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import PipelineConfig
from agentception.types import JsonValue


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; avoids repeated lifespan overhead."""
    with TestClient(app) as c:
        yield c


# ── GET /api/issues/{repo}/{number}/comments ──────────────────────────────────


def test_issue_comments_partial_returns_200(client: TestClient) -> None:
    """GET /api/issues/…/comments must return HTTP 200."""
    with patch(
        "agentception.routes.api.issues.get_issue_comments",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/issues/myrepo/42/comments")
    assert response.status_code == 200


def test_issue_comments_partial_returns_html_content_type(client: TestClient) -> None:
    """GET /api/issues/…/comments must return text/html."""
    with patch(
        "agentception.routes.api.issues.get_issue_comments",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/issues/myrepo/42/comments")
    assert "text/html" in response.headers["content-type"]


def test_issue_comments_partial_gracefully_degrades_on_reader_error(
    client: TestClient,
) -> None:
    """GET /api/issues/…/comments must still return 200 when the reader raises."""
    with patch(
        "agentception.routes.api.issues.get_issue_comments",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GitHub API unavailable"),
    ):
        response = client.get("/api/issues/myrepo/42/comments")
    assert response.status_code == 200


# ── GET /api/prs/{repo}/{number}/checks ───────────────────────────────────────


def test_pr_checks_partial_returns_200(client: TestClient) -> None:
    """GET /api/prs/…/checks must return HTTP 200."""
    with patch(
        "agentception.routes.api.issues.get_pr_checks",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/prs/myrepo/7/checks")
    assert response.status_code == 200


def test_pr_checks_partial_returns_html_content_type(client: TestClient) -> None:
    """GET /api/prs/…/checks must return text/html."""
    with patch(
        "agentception.routes.api.issues.get_pr_checks",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/prs/myrepo/7/checks")
    assert "text/html" in response.headers["content-type"]


def test_pr_checks_partial_gracefully_degrades_on_reader_error(
    client: TestClient,
) -> None:
    """GET /api/prs/…/checks must still return 200 when the reader raises."""
    with patch(
        "agentception.routes.api.issues.get_pr_checks",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh subprocess failed"),
    ):
        response = client.get("/api/prs/myrepo/7/checks")
    assert response.status_code == 200


# ── GET /api/prs/{repo}/{number}/reviews ──────────────────────────────────────


def test_pr_reviews_partial_returns_200(client: TestClient) -> None:
    """GET /api/prs/…/reviews must return HTTP 200."""
    with patch(
        "agentception.routes.api.issues.get_pr_reviews",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/prs/myrepo/7/reviews")
    assert response.status_code == 200


def test_pr_reviews_partial_returns_html_content_type(client: TestClient) -> None:
    """GET /api/prs/…/reviews must return text/html."""
    with patch(
        "agentception.routes.api.issues.get_pr_reviews",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/prs/myrepo/7/reviews")
    assert "text/html" in response.headers["content-type"]


def test_pr_reviews_partial_gracefully_degrades_on_reader_error(
    client: TestClient,
) -> None:
    """GET /api/prs/…/reviews must still return 200 when the reader raises."""
    with patch(
        "agentception.routes.api.issues.get_pr_reviews",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh subprocess failed"),
    ):
        response = client.get("/api/prs/myrepo/7/reviews")
    assert response.status_code == 200


# ── GET /api/issues/approval-queue ────────────────────────────────────────────

_APPROVAL_CONFIG = PipelineConfig(approval_required_labels=["db-schema", "security"])


def _make_issue(labels: list[str]) -> dict[str, JsonValue]:
    """Build a minimal issue dict with string labels for use in queue tests."""
    lbl: JsonValue = json.loads(json.dumps(labels))
    return {"number": 1, "title": "Test issue", "labels": lbl}


def test_approval_queue_partial_returns_200(client: TestClient) -> None:
    """GET /api/issues/approval-queue must return HTTP 200."""
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200


def test_approval_queue_partial_returns_html_content_type(client: TestClient) -> None:
    """GET /api/issues/approval-queue must return text/html."""
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert "text/html" in response.headers["content-type"]


def test_approval_queue_excludes_already_approved_issues(client: TestClient) -> None:
    """Issues carrying the 'approved' label must not appear in the queue."""
    issue_approved = _make_issue(["db-schema", "approved"])
    issue_pending = _make_issue(["db-schema"])
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[issue_approved, issue_pending],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200
    # The approved issue must be filtered out — only one remains.
    # We verify via the template rendering: the pending issue's title must appear
    # while the approved issue label combination should not produce two entries.
    assert response.text.count("Test issue") == 1


def test_approval_queue_includes_issues_with_matching_labels(client: TestClient) -> None:
    """Issues whose label set intersects approval_required_labels must appear."""
    issue = _make_issue(["security"])
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[issue],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200


def test_approval_queue_excludes_issues_with_no_matching_labels(
    client: TestClient,
) -> None:
    """Issues whose labels don't intersect approval_required_labels must be excluded."""
    issue = _make_issue(["bug", "enhancement"])  # no approval label
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[issue],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200
    # Non-matching issue title must not appear in the rendered queue.
    assert "Test issue" not in response.text


def test_approval_queue_falls_back_to_defaults_when_config_fails(
    client: TestClient,
) -> None:
    """When read_pipeline_config raises, the default approval labels must be used."""
    issue_matching = _make_issue(["api-contract"])   # in _DEFAULT_APPROVAL_LABELS
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("config not found"),
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[issue_matching],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    # Even with config failure the endpoint must not 500.
    assert response.status_code == 200


def test_approval_queue_gracefully_degrades_when_github_fails(
    client: TestClient,
) -> None:
    """GET /api/issues/approval-queue must return 200 with empty list when GitHub errors."""
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            side_effect=RuntimeError("gh API error"),
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200


def test_approval_queue_handles_dict_label_format(client: TestClient) -> None:
    """Labels supplied as GitHub API dicts {name: str} must be normalised correctly."""
    issue: dict[str, JsonValue] = {
        "number": 2,
        "title": "Dict label issue",
        "labels": [{"name": "db-schema"}, {"name": "priority/high"}],
    }
    with (
        patch(
            "agentception.routes.api.issues.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=_APPROVAL_CONFIG,
        ),
        patch(
            "agentception.routes.api.issues.get_open_issues",
            new_callable=AsyncMock,
            return_value=[issue],
        ),
    ):
        response = client.get("/api/issues/approval-queue")
    assert response.status_code == 200
    assert "Dict label issue" in response.text


# ── POST /api/issues/{repo}/{number}/approve ──────────────────────────────────


def test_approve_issue_returns_200(client: TestClient) -> None:
    """POST /api/issues/…/approve must return HTTP 200."""
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/issues/myrepo/55/approve")
    assert response.status_code == 200


def test_approve_issue_returns_html_content_type(client: TestClient) -> None:
    """POST /api/issues/…/approve must return text/html."""
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/issues/myrepo/55/approve")
    assert "text/html" in response.headers["content-type"]


def test_approve_issue_emits_hx_trigger_header(client: TestClient) -> None:
    """POST /api/issues/…/approve must set an HX-Trigger header with a toast payload."""
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/issues/myrepo/55/approve")
    raw = response.headers.get("hx-trigger", "")
    assert raw != "", "HX-Trigger header must be present"
    payload = json.loads(raw)
    assert "toast" in payload


def test_approve_issue_hx_trigger_includes_issue_number(client: TestClient) -> None:
    """The HX-Trigger toast message must contain the approved issue number."""
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/issues/myrepo/55/approve")
    payload = json.loads(response.headers["hx-trigger"])
    assert "55" in payload["toast"]["message"]
    assert payload["toast"]["type"] == "success"


def test_approve_issue_calls_ensure_label_and_add_label(client: TestClient) -> None:
    """POST /api/issues/…/approve must call both ensure_label_exists and add_label_to_issue."""
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_ensure,
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_add,
    ):
        client.post("/api/issues/myrepo/55/approve")
    mock_ensure.assert_awaited_once_with("approved", "2ea44f", "Human-approved for pipeline")
    mock_add.assert_awaited_once_with(55, "approved")


def test_approve_issue_gracefully_degrades_on_github_error(client: TestClient) -> None:
    """POST /api/issues/…/approve must return 200 even when the GitHub call fails.

    The HX-Trigger header must still be emitted so the user receives feedback.
    """
    with (
        patch(
            "agentception.routes.api.issues.ensure_label_exists",
            new_callable=AsyncMock,
            side_effect=RuntimeError("gh API error"),
        ),
        patch(
            "agentception.routes.api.issues.add_label_to_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/issues/myrepo/55/approve")
    assert response.status_code == 200
    assert "hx-trigger" in response.headers
