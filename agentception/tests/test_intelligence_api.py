"""Tests for GET /api/intelligence/dag, GET /intelligence/pr-violations,
POST /intelligence/pr-violations/{pr_number}/close, and
POST /analyze/issue/{number} routes.

Every external call (build_dag, detect_out_of_order_prs, close_pr,
analyze_issue) is mocked so these tests run fully offline.

Run targeted:
    pytest agentception/tests/test_intelligence_api.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.analyzer import IssueAnalysis
from agentception.intelligence.dag import DependencyDAG, IssueNode
from agentception.intelligence.guards import PRViolation


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; avoids repeated lifespan overhead."""
    with TestClient(app) as c:
        yield c


# ── GET /api/intelligence/dag ─────────────────────────────────────────────────

_EMPTY_DAG = DependencyDAG(nodes=[], edges=[])

_NODE = IssueNode(
    number=42,
    title="Add feature",
    state="open",
    labels=["ac/5-plan"],
    has_wip=False,
    deps=[10],
)
_DAG_WITH_DEPS = DependencyDAG(nodes=[_NODE], edges=[(42, 10)])


def test_dag_api_returns_200(client: TestClient) -> None:
    """GET /api/intelligence/dag must return HTTP 200."""
    with patch(
        "agentception.routes.intelligence.build_dag",
        new_callable=AsyncMock,
        return_value=_EMPTY_DAG,
    ):
        response = client.get("/api/intelligence/dag")
    assert response.status_code == 200


def test_dag_api_returns_nodes_and_edges_keys(client: TestClient) -> None:
    """GET /api/intelligence/dag response body must have 'nodes' and 'edges' keys."""
    with patch(
        "agentception.routes.intelligence.build_dag",
        new_callable=AsyncMock,
        return_value=_EMPTY_DAG,
    ):
        body = client.get("/api/intelligence/dag").json()
    assert "nodes" in body
    assert "edges" in body


def test_dag_api_returns_correct_node_shape(client: TestClient) -> None:
    """GET /api/intelligence/dag nodes must include number, title, state, labels, has_wip, deps."""
    with patch(
        "agentception.routes.intelligence.build_dag",
        new_callable=AsyncMock,
        return_value=_DAG_WITH_DEPS,
    ):
        body = client.get("/api/intelligence/dag").json()
    assert len(body["nodes"]) == 1
    node = body["nodes"][0]
    assert node["number"] == 42
    assert node["title"] == "Add feature"
    assert node["has_wip"] is False
    assert node["deps"] == [10]


def test_dag_api_returns_correct_edges(client: TestClient) -> None:
    """GET /api/intelligence/dag edges must reflect dependency pairs as [from, to] tuples."""
    with patch(
        "agentception.routes.intelligence.build_dag",
        new_callable=AsyncMock,
        return_value=_DAG_WITH_DEPS,
    ):
        body = client.get("/api/intelligence/dag").json()
    assert body["edges"] == [[42, 10]]


# ── GET /api/intelligence/pr-violations ───────────────────────────────────────

_VIOLATION = PRViolation(
    pr_number=7,
    pr_title="Fix old thing",
    expected_label="ac/5-plan",
    actual_label="ac/3-implement",
    linked_issue=99,
)


def test_pr_violations_api_returns_200(client: TestClient) -> None:
    """GET /api/intelligence/pr-violations must return HTTP 200."""
    with patch(
        "agentception.routes.api.intelligence.detect_out_of_order_prs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/intelligence/pr-violations")
    assert response.status_code == 200


def test_pr_violations_api_returns_empty_list_when_no_violations(client: TestClient) -> None:
    """GET /api/intelligence/pr-violations must return [] when nothing is out of order."""
    with patch(
        "agentception.routes.api.intelligence.detect_out_of_order_prs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        body = client.get("/api/intelligence/pr-violations").json()
    assert body == []


def test_pr_violations_api_returns_violation_objects(client: TestClient) -> None:
    """GET /api/intelligence/pr-violations must return serialised PRViolation entries."""
    with patch(
        "agentception.routes.api.intelligence.detect_out_of_order_prs",
        new_callable=AsyncMock,
        return_value=[_VIOLATION],
    ):
        body = client.get("/api/intelligence/pr-violations").json()
    assert len(body) == 1
    v = body[0]
    assert v["pr_number"] == 7
    assert v["expected_label"] == "ac/5-plan"
    assert v["actual_label"] == "ac/3-implement"
    assert v["linked_issue"] == 99


# ── POST /api/intelligence/pr-violations/{pr_number}/close ────────────────────


def test_close_violating_pr_returns_closed_key(client: TestClient) -> None:
    """POST …/close must return {closed: pr_number} on success."""
    with patch(
        "agentception.routes.api.intelligence.close_pr",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = client.post("/api/intelligence/pr-violations/7/close")
    assert response.status_code == 200
    assert response.json() == {"closed": 7}


def test_close_violating_pr_runtime_error_returns_500(client: TestClient) -> None:
    """POST …/close must return HTTP 500 when close_pr raises RuntimeError."""
    with patch(
        "agentception.routes.api.intelligence.close_pr",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh pr close failed"),
    ):
        response = client.post("/api/intelligence/pr-violations/7/close")
    assert response.status_code == 500
    assert "7" in response.json()["detail"]


def test_close_violating_pr_includes_pr_number_in_error_detail(client: TestClient) -> None:
    """The 500 detail must embed the PR number so operators know which PR failed."""
    with patch(
        "agentception.routes.api.intelligence.close_pr",
        new_callable=AsyncMock,
        side_effect=RuntimeError("network failure"),
    ):
        response = client.post("/api/intelligence/pr-violations/123/close")
    assert response.status_code == 500
    assert "123" in response.json()["detail"]


# ── POST /api/analyze/issue/{number} ──────────────────────────────────────────

_ANALYSIS = IssueAnalysis(
    number=42,
    dependencies=[],
    parallelism="safe",
    conflict_risk="none",
    modifies_files=[],
    recommended_role="python-developer",
    recommended_merge_after=None,
)


def test_analyze_issue_api_returns_200(client: TestClient) -> None:
    """POST /api/analyze/issue/{number} must return HTTP 200 on success."""
    with patch(
        "agentception.routes.api.intelligence.analyze_issue",
        new_callable=AsyncMock,
        return_value=_ANALYSIS,
    ):
        response = client.post("/api/analyze/issue/42")
    assert response.status_code == 200


def test_analyze_issue_api_returns_issue_analysis_shape(client: TestClient) -> None:
    """POST /api/analyze/issue/{number} response must include all IssueAnalysis fields."""
    with patch(
        "agentception.routes.api.intelligence.analyze_issue",
        new_callable=AsyncMock,
        return_value=_ANALYSIS,
    ):
        body = client.post("/api/analyze/issue/42").json()
    assert body["number"] == 42
    assert body["parallelism"] == "safe"
    assert body["conflict_risk"] == "none"
    assert body["recommended_role"] == "python-developer"
    assert body["recommended_merge_after"] is None


def test_analyze_issue_api_not_found_returns_404(client: TestClient) -> None:
    """POST /api/analyze/issue/{number} must return 404 when error contains 'not found'."""
    with patch(
        "agentception.routes.api.intelligence.analyze_issue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("issue not found"),
    ):
        response = client.post("/api/analyze/issue/999")
    assert response.status_code == 404


def test_analyze_issue_api_other_error_returns_500(client: TestClient) -> None:
    """POST /api/analyze/issue/{number} must return 500 for non-404 errors."""
    with patch(
        "agentception.routes.api.intelligence.analyze_issue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh subprocess exited 1"),
    ):
        response = client.post("/api/analyze/issue/42")
    assert response.status_code == 500


def test_analyze_issue_api_error_detail_is_propagated(client: TestClient) -> None:
    """The runtime error message must appear verbatim in the 500 response detail."""
    msg = "gh subprocess exited 1: permission denied"
    with patch(
        "agentception.routes.api.intelligence.analyze_issue",
        new_callable=AsyncMock,
        side_effect=RuntimeError(msg),
    ):
        response = client.post("/api/analyze/issue/42")
    assert msg in response.json()["detail"]
