from __future__ import annotations

"""Tests for the AgentCeption agent detail UI and API endpoints (AC-007).

Covers:
- ``GET /agents/{id}`` — HTML agent detail page
- ``GET /api/agents`` — list of root-level AgentNodes
- ``GET /api/agents/{id}`` — single AgentNode by ID
- ``GET /api/agents/{id}/transcript`` — parsed transcript messages

All tests are synchronous with mocked state — no live GitHub calls, no
background polling, no filesystem reads.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_agentception_ui_agent.py -v
"""

import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    """A single AgentNode with known transcript path for testing."""
    return AgentNode(
        id="issue-616",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=616,
        branch="feat/issue-616-agent-inspector-ui",
        batch_id="eng-20260302T013317Z-4e62",
        worktree_path="/worktrees/issue-616",
        transcript_path="/tmp/fake-transcript.jsonl",
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
        transcript_path=None,
    )
    parent = AgentNode(
        id="issue-616",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=616,
        message_count=3,
        transcript_path="/tmp/fake-616.jsonl",
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


def test_agent_detail_renders_transcript(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> must render HTTP 200 with transcript for a known agent ID."""
    fake_messages = [
        {"role": "user", "text": "Implement issue 616 now."},
        {"role": "assistant", "text": "On it. Reading the issue..."},
    ]
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=fake_messages,
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "developer" in response.text
    assert "Implement issue 616 now." in response.text
    assert "On it. Reading the issue" in response.text


def test_agent_detail_renders_child_agent(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<child-id> must resolve child agents one level deep."""
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/agents/child-abc")
    assert response.status_code == 200
    assert "reviewer" in response.text


def test_agent_detail_no_transcript_path(client: TestClient) -> None:
    """GET /agents/<id> must render 200 and show empty transcript when transcript_path is None."""
    node = AgentNode(
        id="no-transcript",
        role="unknown",
        status=AgentStatus.FAILED,
        transcript_path=None,
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
        response = client.get("/agents/no-transcript")
    assert response.status_code == 200
    # The page renders successfully regardless of transcript content.
    assert "no-transcript" in response.text


def test_agent_detail_contains_task_table(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include the task-table section."""
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "task-table" in response.text
    assert "agent-task" in response.text


def test_agent_detail_contains_breadcrumb(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include a breadcrumb link back to overview."""
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    # Breadcrumb now uses history.back() with "← Back" label.
    assert "← Back" in response.text
    assert "back-link" in response.text


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
    data = response.json()
    assert data == []


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


def test_transcript_api_returns_list(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /api/agents/<id>/transcript must return a JSON list of message dicts."""
    fake_messages = [
        {"role": "user", "text": "Start implementing."},
        {"role": "assistant", "text": "Starting now."},
    ]
    with (
        patch("agentception.routes.api.pipeline.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.api.pipeline.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=fake_messages,
        ),
    ):
        response = client.get("/api/agents/issue-616/transcript")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["role"] == "user"
    assert data[1]["role"] == "assistant"


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


def test_transcript_api_empty_when_no_path(
    client: TestClient,
) -> None:
    """GET /api/agents/<id>/transcript must return [] when the agent has no transcript_path."""
    node = AgentNode(
        id="notranscript",
        role="unknown",
        status=AgentStatus.FAILED,
        transcript_path=None,
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
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/notranscript/transcript")
    assert response.status_code == 200
    assert response.json() == []


# ── New tests for issue #741 ───────────────────────────────────────────────────


def test_transcript_not_truncated(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> must render full message content — no 400-char truncation."""
    long_text = "A" * 1000
    fake_messages = [{"role": "assistant", "text": long_text}]
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=fake_messages,
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    # All 1000 characters must appear in the rendered HTML — no truncation at 400.
    assert long_text in response.text


def test_transcript_partial_returns_200(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /partials/agents/<id>/transcript must return HTTP 200 with message HTML."""
    fake_messages = [
        {"role": "user", "text": "Start."},
        {"role": "assistant", "text": "Done."},
    ]
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=fake_messages,
        ),
    ):
        response = client.get("/partials/agents/issue-616/transcript")
    assert response.status_code == 200
    assert "message--user" in response.text
    assert "message--assistant" in response.text
    assert "Start." in response.text
    assert "Done." in response.text


def test_transcript_partial_empty_when_no_messages(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /partials/agents/<id>/transcript with no messages shows empty state."""
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/partials/agents/issue-616/transcript")
    assert response.status_code == 200
    assert "No transcript available" in response.text


def test_agent_detail_has_copy_buttons(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include copy buttons (btn-copy class)."""
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    assert "btn-copy" in response.text


def test_agent_detail_has_conversation_section(
    client: TestClient, pipeline_with_agent: PipelineState
) -> None:
    """GET /agents/<id> HTML must include the collapsible Conversation section when messages exist."""
    fake_messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    with (
        patch("agentception.routes.ui.agents.get_state", return_value=pipeline_with_agent),
        patch(
            "agentception.routes.ui.agents.read_transcript_messages",
            new_callable=AsyncMock,
            return_value=fake_messages,
        ),
    ):
        response = client.get("/agents/issue-616")
    assert response.status_code == 200
    # Conversation renders in a collapsible <details> element when messages are present.
    assert "Conversation" in response.text
    assert "agent-section-disclosure" in response.text
