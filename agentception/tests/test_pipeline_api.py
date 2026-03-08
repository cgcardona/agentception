"""Tests for agentception/routes/api/pipeline.py and routes/ui/_shared._find_agent.

Covers every route handler and the shared tree-search helper:

    GET  /pipeline
    GET  /agents
    GET  /agents/{agent_id}
    GET  /agents/{agent_id}/transcript
    _find_agent (unit)

Run targeted:
    pytest agentception/tests/test_pipeline_api.py -v
"""
from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import AgentNode, AgentStatus, PipelineState
from agentception.routes.ui._shared import _find_agent


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


def _make_state(
    *,
    active_label: str | None = "phase/0",
    agents: list[AgentNode] | None = None,
) -> PipelineState:
    return PipelineState(
        active_label=active_label,
        issues_open=3,
        prs_open=1,
        agents=agents or [],
        alerts=[],
        polled_at=time.time(),
    )


def _make_agent(
    agent_id: str = "issue-42",
    *,
    transcript_path: str | None = None,
    children: list[AgentNode] | None = None,
) -> AgentNode:
    return AgentNode(
        id=agent_id,
        role="python-developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=42,
        transcript_path=transcript_path,
        children=children or [],
    )


# ── GET /pipeline ─────────────────────────────────────────────────────────────


def test_pipeline_api_returns_200(client: TestClient) -> None:
    """GET /api/pipeline must respond HTTP 200."""
    state = _make_state()
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/pipeline")
    assert response.status_code == 200


def test_pipeline_api_returns_pipeline_state_shape(client: TestClient) -> None:
    """GET /api/pipeline response must contain PipelineState fields."""
    state = _make_state()
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/pipeline")
    body = response.json()
    assert "active_label" in body
    assert "issues_open" in body
    assert "agents" in body
    assert body["issues_open"] == 3
    assert body["active_label"] == "phase/0"


def test_pipeline_api_returns_empty_state_when_poller_not_ready(
    client: TestClient,
) -> None:
    """GET /api/pipeline must fall back to PipelineState.empty() when get_state returns None."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=None):
        response = client.get("/api/pipeline")
    assert response.status_code == 200
    body = response.json()
    assert body["active_label"] is None
    assert body["issues_open"] == 0
    assert body["agents"] == []


# ── GET /api/agents ───────────────────────────────────────────────────────────


def test_agents_api_returns_200(client: TestClient) -> None:
    """GET /api/agents must respond HTTP 200."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=_make_state()):
        response = client.get("/api/agents")
    assert response.status_code == 200


def test_agents_api_returns_empty_list_when_no_state(client: TestClient) -> None:
    """GET /api/agents returns [] when the poller has not completed its first tick."""
    with patch("agentception.routes.api.pipeline.get_state", return_value=None):
        response = client.get("/api/agents")
    assert response.status_code == 200
    assert response.json() == []


def test_agents_api_returns_agents_list(client: TestClient) -> None:
    """GET /api/agents returns the agents embedded in the current state."""
    agent = _make_agent("issue-99")
    state = _make_state(agents=[agent])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents")
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == "issue-99"
    assert body[0]["role"] == "python-developer"


# ── GET /api/agents/{agent_id} ────────────────────────────────────────────────


def test_agent_api_returns_200_for_known_agent(client: TestClient) -> None:
    """GET /api/agents/{id} returns HTTP 200 when the agent exists."""
    agent = _make_agent("issue-42")
    state = _make_state(agents=[agent])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/issue-42")
    assert response.status_code == 200
    assert response.json()["id"] == "issue-42"


def test_agent_api_returns_404_when_agent_not_found(client: TestClient) -> None:
    """GET /api/agents/{id} returns HTTP 404 with a descriptive message for unknown IDs."""
    state = _make_state(agents=[])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/ghost-99")
    assert response.status_code == 404
    assert "ghost-99" in response.json()["detail"]


def test_agent_api_finds_root_agent(client: TestClient) -> None:
    """GET /api/agents/{id} resolves root-level agents by ID."""
    agent_a = _make_agent("issue-1")
    agent_b = _make_agent("issue-2")
    state = _make_state(agents=[agent_a, agent_b])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/issue-2")
    assert response.status_code == 200
    assert response.json()["id"] == "issue-2"


def test_agent_api_finds_child_agent(client: TestClient) -> None:
    """GET /api/agents/{id} resolves child agents nested inside a root agent."""
    child = _make_agent("issue-child-5")
    parent = _make_agent("issue-parent-1", children=[child])
    state = _make_state(agents=[parent])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/issue-child-5")
    assert response.status_code == 200
    assert response.json()["id"] == "issue-child-5"


# ── GET /api/agents/{agent_id}/transcript ────────────────────────────────────


def test_transcript_api_returns_404_when_agent_not_found(client: TestClient) -> None:
    """GET /api/agents/{id}/transcript returns HTTP 404 when the agent ID is unknown."""
    state = _make_state(agents=[])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/ghost-99/transcript")
    assert response.status_code == 404
    assert "ghost-99" in response.json()["detail"]


def test_transcript_api_returns_empty_list_when_no_transcript_path(
    client: TestClient,
) -> None:
    """GET /api/agents/{id}/transcript returns [] when the agent has no transcript file set."""
    agent = _make_agent("issue-55", transcript_path=None)
    state = _make_state(agents=[agent])
    with patch("agentception.routes.api.pipeline.get_state", return_value=state):
        response = client.get("/api/agents/issue-55/transcript")
    assert response.status_code == 200
    assert response.json() == []


def test_transcript_api_returns_messages(client: TestClient, tmp_path: str) -> None:
    """GET /api/agents/{id}/transcript delegates to read_transcript_messages and returns results."""
    messages = [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "world"},
    ]
    agent = _make_agent("issue-77", transcript_path="/fake/path.jsonl")
    state = _make_state(agents=[agent])
    mock_reader = AsyncMock(return_value=messages)
    with (
        patch("agentception.routes.api.pipeline.get_state", return_value=state),
        patch("agentception.routes.api.pipeline.read_transcript_messages", mock_reader),
    ):
        response = client.get("/api/agents/issue-77/transcript")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0] == {"role": "user", "text": "hello"}
    assert body[1] == {"role": "assistant", "text": "world"}


def test_transcript_api_passes_path_to_reader(client: TestClient) -> None:
    """GET /api/agents/{id}/transcript converts transcript_path to a Path before calling reader."""
    from pathlib import Path

    agent = _make_agent("issue-88", transcript_path="/tmp/agent.jsonl")
    state = _make_state(agents=[agent])
    mock_reader = AsyncMock(return_value=[])
    with (
        patch("agentception.routes.api.pipeline.get_state", return_value=state),
        patch("agentception.routes.api.pipeline.read_transcript_messages", mock_reader),
    ):
        client.get("/api/agents/issue-88/transcript")
    mock_reader.assert_awaited_once_with(Path("/tmp/agent.jsonl"))


# ── _find_agent unit tests ─────────────────────────────────────────────────────


def test_find_agent_returns_none_when_state_is_none() -> None:
    """`_find_agent` returns None gracefully when no state is available."""
    assert _find_agent(None, "issue-1") is None


def test_find_agent_returns_none_when_id_not_in_state() -> None:
    """`_find_agent` returns None when the ID is absent from both root and children."""
    state = _make_state(agents=[_make_agent("issue-10")])
    assert _find_agent(state, "issue-99") is None


def test_find_agent_finds_root_agent() -> None:
    """`_find_agent` returns the correct root AgentNode by ID."""
    agent = _make_agent("issue-7")
    state = _make_state(agents=[agent])
    result = _find_agent(state, "issue-7")
    assert result is not None
    assert result.id == "issue-7"


def test_find_agent_finds_child_agent() -> None:
    """`_find_agent` descends one level into children and returns the matching node."""
    child = _make_agent("issue-child-3")
    parent = _make_agent("issue-parent-1", children=[child])
    state = _make_state(agents=[parent])
    result = _find_agent(state, "issue-child-3")
    assert result is not None
    assert result.id == "issue-child-3"


def test_find_agent_prefers_root_over_child_with_same_id() -> None:
    """`_find_agent` returns the root match first when root and child share an ID.

    This is an edge case that cannot occur in production (IDs are unique worktree
    basenames), but the behaviour must be deterministic and documented.
    """
    child = _make_agent("duplicate-id")
    parent = _make_agent("issue-root-1", children=[child])
    root_duplicate = _make_agent("duplicate-id")
    # root_duplicate appears first in the list
    state = _make_state(agents=[root_duplicate, parent])
    result = _find_agent(state, "duplicate-id")
    assert result is not None
    assert result.id == "duplicate-id"
    # The root agent has no children — confirms we returned the root, not the child
    assert result.children == []
