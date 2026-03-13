from __future__ import annotations

"""Tests for the AgentCeption agent detail UI and API endpoints (AC-007).

Covers:
- ``GET /agents/{id}`` — HTML agent detail page
- ``GET /api/agents`` — list of root-level AgentNodes
- ``GET /api/agents/{id}`` — single AgentNode by ID
- ``GET /api/agents/{id}/transcript`` — transcript endpoint (returns [] until DB reader is wired)
- ``GET /partials/agents/{id}/transcript`` — transcript partial (empty state)

All tests are synchronous with mocked state — no live GitHub calls, no
background polling, no filesystem reads.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_agentception_ui_agent.py -v
"""

import time
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import AgentNode, AgentStatus, PipelineState


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with lifespan (poller started then immediately cancelled)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def agent_node() -> AgentNode:
    """A single AgentNode for testing."""
    return AgentNode(
        id="issue-616",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=616,
        branch="feat/issue-616-agent-inspector-ui",
        batch_id="eng-20260302T013317Z-4e62",
        worktree_path="/worktrees/issue-616",
        message_count=3,
    )


@pytest.fixture()
def pipeline_with_agent(agent_node: AgentNode) -> PipelineState:
    """PipelineState with one root agent and one child agent."""
    child = AgentNode(
        id="child-abc",
        role="reviewer",
        status=AgentStatus.REVIEWING,
        message_count=1,
    )
    parent = AgentNode(
        id="issue-616",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=616,
        message_count=3,
        children=[child],
    )
    return PipelineState(
        active_label="agentception/0-scaffold",
        issues_open=1,
        prs_open=0,
        agents=[parent],
        alerts=[],
        polled_at=time.time(),
    )


# ── GET /agents/{id} — HTML detail page ───────────────────────────────────────


def test_agent_detail_404_unknown_id(client: TestClient) -> None:
    """GET /agents/<unknown> must return HTTP 404 when the agent ID is not in state."""
    state = PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )
    with patch("agentception.routes.ui.agents.get_state", return_value=state):
        response = client.get("/agents/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.text


def test_agent_detail_renders_200(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> must render HTTP 200 for a known agent ID."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "developer" in response.text


def test_agent_detail_renders_child_agent(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<child-id> must resolve child agents one level deep."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/agents/child-abc")
    assert response.status_code == 200
    assert "reviewer" in response.text


def test_agent_detail_renders_without_messages(client: TestClient) -> None:
    """GET /agents/<id> must render 200 when no messages are available."""
    node = AgentNode(
        id="no-messages",
        role="unknown",
        status=AgentStatus.FAILED,
        message_count=0,
    )
    state = PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[node],
        alerts=[],
        polled_at=time.time(),
    )
    with patch("agentception.routes.ui.agents.get_state", return_value=state):
        response = client.get("/agents/no-messages")
    assert response.status_code == 200
    assert "no-messages" in response.text


def test_agent_detail_contains_task_table(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include the task-table section."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "task-table" in response.text
    assert "agent-task" in response.text


def test_agent_detail_contains_breadcrumb(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include a breadcrumb link back to overview."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "← Back" in response.text
    assert "back-link" in response.text


def test_agent_detail_has_copy_buttons(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include copy buttons (btn-copy class)."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "btn-copy" in response.text


# ── GET /api/agents — list endpoint ───────────────────────────────────────────


def test_agents_list_api_returns_array(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /api/agents must return a JSON array of AgentNodes."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=pipeline_with_agent):
        response = client.get("/api/agents")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["id"] == "issue-616"
    assert data[0]["role"] == "developer"


def test_agents_list_api_empty_state(client: TestClient) -> None:
    """GET /api/agents must return an empty array when no agents are active."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=None):
        response = client.get("/api/agents")
    assert response.status_code == 200
    assert response.json() == []


# ── GET /api/agents/{id} — single agent ───────────────────────────────────────


def test_agent_api_returns_node(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /api/agents/<id> must return the matching AgentNode as JSON."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=pipeline_with_agent):
        response = client.get("/api/agents/issue-616")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "issue-616"
    assert data["status"] == "implementing"


def test_agent_api_404_unknown(client: TestClient) -> None:
    """GET /api/agents/<unknown> must return HTTP 404."""
    state = PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/not-real")
    assert response.status_code == 404


# ── GET /api/agents/{id}/transcript — transcript endpoint ─────────────────────


def test_transcript_api_returns_empty_list(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /api/agents/<id>/transcript returns [] until DB reader is wired."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=pipeline_with_agent):
        response = client.get("/api/agents/issue-616/transcript")
    assert response.status_code == 200
    assert response.json() == []


def test_transcript_api_404_unknown(client: TestClient) -> None:
    """GET /api/agents/<unknown>/transcript must return HTTP 404."""
    state = PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/ghost/transcript")
    assert response.status_code == 404


# ── GET /partials/agents/{id}/transcript — transcript partial ─────────────────


def test_transcript_partial_returns_200(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /partials/agents/<id>/transcript must return HTTP 200."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/partials/agents/issue-616/transcript")
    assert response.status_code == 200


def test_transcript_partial_empty_shows_empty_state(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /partials/agents/<id>/transcript shows empty state when no messages."""
    with patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent):
        response = client.get("/partials/agents/issue-616/transcript")
    assert response.status_code == 200
    assert "No transcript available" in response.text
