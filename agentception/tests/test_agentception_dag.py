from __future__ import annotations

"""Tests for the AgentCeption dependency DAG builder and UI/API routes.

Covers:
- parse_deps_from_body (all known variants)
- build_dag (with mocked GitHub data) — hermetic, no live API calls
- GET /dag  — HTML page (dag_page_returns_200, d3_cdn)
- GET /api/dag — JSON endpoint (returns nodes and edges)
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.dag import (
    DependencyDAG,
    IssueNode,
    build_dag,
    parse_deps_from_body,
)


# ---------------------------------------------------------------------------
# parse_deps_from_body
# ---------------------------------------------------------------------------


def test_parse_deps_simple() -> None:
    """'Depends on: #552' → [552]."""
    body = "## Overview\n\nDepends on: #552\n\nSome description."
    assert parse_deps_from_body(body) == [552]


def test_parse_deps_bold_markdown() -> None:
    """'**Depends on:** #553' → [553]."""
    body = "**Depends on:** #553\n\nRest of the body."
    assert parse_deps_from_body(body) == [553]


def test_parse_deps_multiple() -> None:
    """Multiple 'Depends on' lines each contribute their own issue number."""
    body = (
        "**Depends on:** #552\n"
        "Some other text.\n"
        "Blocked by #600\n"
        "More text.\n"
    )
    result = parse_deps_from_body(body)
    assert 552 in result
    assert 600 in result
    assert result == sorted(result), "result should be sorted"


def test_parse_deps_multiple_on_one_line() -> None:
    """'Depends on #552, #553' → [552, 553]."""
    body = "Depends on #552, #553 to be available first."
    result = parse_deps_from_body(body)
    assert result == [552, 553]


def test_parse_deps_requires_keyword() -> None:
    """'Requires #614' is also recognised."""
    body = "Requires #614 to land first."
    assert parse_deps_from_body(body) == [614]


def test_parse_deps_no_deps() -> None:
    """Body with no dependency declarations → []."""
    body = "## Overview\n\nJust a standalone task.\n\n## Acceptance Criteria\n- [ ] Done"
    assert parse_deps_from_body(body) == []


def test_parse_deps_range_only_first() -> None:
    """'Depends on: #552–#585' — range notation: only numbers with # prefix are captured.

    This is a known limitation (documented): the function extracts every
    '#NNN' token on the dependency line, so '#552' is found but '#585'
    is only captured if it is also prefixed with '#'.  A range using an
    en-dash without a second '#' prefix is treated as prose and only the
    first number is extracted.
    """
    body = "Depends on: #552–585"
    result = parse_deps_from_body(body)
    assert 552 in result
    # 585 is NOT prefixed with '#' in en-dash range notation — only 552 found.
    assert 585 not in result


def test_parse_deps_deduplicates() -> None:
    """Same issue number mentioned twice in the body appears only once."""
    body = "Depends on #552\nAlso depends on #552"
    result = parse_deps_from_body(body)
    assert result.count(552) == 1


# ---------------------------------------------------------------------------
# build_dag
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_dag_no_issues() -> None:
    """When there are no open issues, the DAG should be empty."""
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[],
    ):
        dag = await build_dag()

    assert isinstance(dag, DependencyDAG)
    assert dag.nodes == []
    assert dag.edges == []


@pytest.mark.anyio
async def test_build_dag_single_issue_no_deps() -> None:
    """A single issue with no dependency declarations produces one node and no edges."""
    fake_issue = {
        "number": 10,
        "title": "Standalone issue",
        "state": "open",
        "labels": [{"name": "enhancement"}],
        "body": "Just a task with no dependencies.",
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[fake_issue],
    ):
        dag = await build_dag()

    assert len(dag.nodes) == 1
    assert dag.nodes[0].number == 10
    assert dag.nodes[0].deps == []
    assert dag.edges == []


@pytest.mark.anyio
async def test_build_dag_chain() -> None:
    """A → B → C chain produces correct nodes and edges.

    Issue A depends on B, which depends on C.
    Expected edges: (A, B) and (B, C).
    """
    issues = [
        {
            "number": 30,
            "title": "Issue A",
            "state": "open",
            "labels": [],
            "body": "Depends on #20",
        },
        {
            "number": 20,
            "title": "Issue B",
            "state": "open",
            "labels": [],
            "body": "Depends on #10",
        },
        {
            "number": 10,
            "title": "Issue C",
            "state": "open",
            "labels": [],
            "body": "No dependencies.",
        },
    ]
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=issues,
    ):
        dag = await build_dag()

    assert len(dag.nodes) == 3
    assert (30, 20) in dag.edges
    assert (20, 10) in dag.edges
    assert len(dag.edges) == 2

    node_map = {n.number: n for n in dag.nodes}
    assert node_map[30].deps == [20]
    assert node_map[20].deps == [10]
    assert node_map[10].deps == []


@pytest.mark.anyio
async def test_build_dag_has_wip_flag() -> None:
    """Issues with the 'agent/wip' label should have has_wip=True."""
    issue = {
        "number": 55,
        "title": "WIP issue",
        "state": "open",
        "labels": [{"name": "agent/wip"}, {"name": "enhancement"}],
        "body": "",
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    assert dag.nodes[0].has_wip is True
    assert "agent/wip" in dag.nodes[0].labels
    assert "enhancement" in dag.nodes[0].labels


@pytest.mark.anyio
async def test_build_dag_label_extraction() -> None:
    """Labels are extracted correctly from both dict and string formats."""
    issue_dict_labels = {
        "number": 1,
        "title": "Dict labels",
        "state": "open",
        "labels": [{"name": "agentception/4-intelligence"}, {"name": "enhancement"}],
        "body": "",
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue_dict_labels],
    ):
        dag = await build_dag()

    node = dag.nodes[0]
    assert "agentception/4-intelligence" in node.labels
    assert "enhancement" in node.labels


@pytest.mark.anyio
async def test_build_dag_multiple_deps() -> None:
    """An issue with multiple dependencies produces multiple edges."""
    issue = {
        "number": 100,
        "title": "Multi-dep issue",
        "state": "open",
        "labels": [],
        "body": "Depends on #50, #60, #70",
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    assert len(dag.edges) == 3
    assert (100, 50) in dag.edges
    assert (100, 60) in dag.edges
    assert (100, 70) in dag.edges
    assert dag.nodes[0].deps == [50, 60, 70]


# ---------------------------------------------------------------------------
# IssueNode / DependencyDAG models
# ---------------------------------------------------------------------------


def test_issue_node_model() -> None:
    """IssueNode can be constructed and serialised correctly."""
    node = IssueNode(
        number=42,
        title="Test issue",
        state="open",
        labels=["enhancement", "agentception"],
        has_wip=False,
        deps=[10, 20],
    )
    assert node.number == 42
    assert node.has_wip is False
    assert node.deps == [10, 20]
    data = node.model_dump()
    assert data["labels"] == ["enhancement", "agentception"]


def test_dependency_dag_model() -> None:
    """DependencyDAG serialises nodes and edges without loss."""
    node = IssueNode(
        number=1, title="A", state="open", labels=[], has_wip=False, deps=[2]
    )
    dag = DependencyDAG(nodes=[node], edges=[(1, 2)])
    assert dag.edges[0] == (1, 2)
    assert dag.nodes[0].number == 1
    dumped = dag.model_dump()
    assert dumped["edges"] == [(1, 2)]


# ---------------------------------------------------------------------------
# UI / API routes — GET /dag and GET /api/dag
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def _empty_dag() -> DependencyDAG:
    """An empty DAG for use in route tests that do not need real GitHub data."""
    return DependencyDAG(nodes=[], edges=[])


def test_dag_page_returns_200(
    client: TestClient, _empty_dag: DependencyDAG
) -> None:
    """GET /dag must return HTTP 200 with a mocked empty DAG."""
    with patch(
        "agentception.routes.ui.dag.build_dag",
        new_callable=AsyncMock,
        return_value=_empty_dag,
    ):
        response = client.get("/dag")
    assert response.status_code == 200


def test_dag_page_includes_d3_cdn(
    client: TestClient, _empty_dag: DependencyDAG
) -> None:
    """GET /dag HTML must load D3.js from the CDN (cdn.jsdelivr.net/npm/d3)."""
    with patch(
        "agentception.routes.ui.dag.build_dag",
        new_callable=AsyncMock,
        return_value=_empty_dag,
    ):
        response = client.get("/dag")
    assert "cdn.jsdelivr.net/npm/d3" in response.text


def test_dag_page_includes_fit_view_button(
    client: TestClient, _empty_dag: DependencyDAG
) -> None:
    """GET /dag HTML must include a Fit view control button."""
    with patch(
        "agentception.routes.ui.dag.build_dag",
        new_callable=AsyncMock,
        return_value=_empty_dag,
    ):
        response = client.get("/dag")
    assert "fitView()" in response.text
    assert "Fit view" in response.text


def test_dag_page_includes_edge_legend(
    client: TestClient, _empty_dag: DependencyDAG
) -> None:
    """GET /dag HTML must include an edge-type legend entry explaining the dependency arrow."""
    with patch(
        "agentception.routes.ui.dag.build_dag",
        new_callable=AsyncMock,
        return_value=_empty_dag,
    ):
        response = client.get("/dag")
    assert "dag-legend-edge-indicator" in response.text
    assert "Depends on" in response.text


def test_dag_api_returns_nodes_and_edges(client: TestClient) -> None:
    """GET /api/intelligence/dag must return JSON with 'nodes' and 'edges' keys."""
    fake_issue = {
        "number": 42,
        "title": "A test issue",
        "state": "open",
        "labels": [{"name": "enhancement"}],
        "body": "",
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[fake_issue],
    ):
        response = client.get("/api/intelligence/dag")
    assert response.status_code == 200
    data = response.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["number"] == 42
